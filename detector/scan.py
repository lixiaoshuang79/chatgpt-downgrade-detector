"""并发检测调度：串行（主实例单出口）或并行（临时 mihomo 实例池，多出口）。

CLI 与 Web GUI 共用入口。并行时启动 N 个临时 mihomo 实例（各自独立
出口与代理端口，全部只监听 127.0.0.1），节点 round-robin 分给各出口，
每实例一个 worker 线程串行检测，结果按输入节点顺序返回。
并行模式完全不触碰正在运行的 Clash Verge 主实例。

（临时实例从 Clash Verge 的合并配置 clash-verge.yaml 复制而来，
  实例池管理见 mihomo_pool.py）
"""
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from clash_api import ClashAPI
from cdp_tester import HeadlessChrome, test_node
from mihomo_pool import MihomoPool


def _worker(api, proxy_port, chrome_path, sleep_sec, node_list,
            results, seq_holder, lock, on_result, worker_id, stop_flag=None):
    with HeadlessChrome(proxy_port=proxy_port, chrome_path=chrome_path) as chrome:
        for node in node_list:
            if stop_flag and stop_flag():
                break
            try:
                ok = api.switch_global(node)
                if not ok:
                    r = {"node": node, "verdict": "ERROR", "reply": "switch failed"}
                else:
                    if sleep_sec > 0:
                        time.sleep(sleep_sec)
                    r = test_node(chrome, node)
            except Exception as e:
                r = {"node": node, "verdict": "ERROR", "reply": str(e)[:80]}
            with lock:
                results.append(r)
                seq_holder[0] += 1
                seq = seq_holder[0]
            if on_result:
                on_result(node, r, worker_id, seq)


def scan_nodes(api, proxy_port, nodes, parallel=1, chrome_path=None,
               sleep_sec=3.0, on_result=None, stop_flag=None) -> list:
    """检测 nodes，返回结果列表（按输入顺序）。on_result(node, result, worker_id, seq) 可选。

    parallel == 1：串行使用传入的主实例（api/proxy_port）。
    parallel >= 2：全部使用临时 mihomo 实例，主实例完全不参与。
    stop_flag：可调用对象，返回 True 时提前停止（GUI 停止按钮）。
    """
    parallel = max(1, int(parallel))
    if parallel == 1 or len(nodes) <= 1:
        results, seq_holder = [], [0]
        lock = threading.Lock()
        _worker(api, proxy_port, chrome_path, sleep_sec, nodes,
                results, seq_holder, lock, on_result, 0, stop_flag)
    else:
        pool = MihomoPool(parallel)
        try:
            instances = pool.start()
            workers = [(ClashAPI(host="127.0.0.1", port=i.ctl_port), i.mixed_port)
                       for i in instances]
            slices = [[] for _ in workers]
            for i, n in enumerate(nodes):
                slices[i % len(workers)].append(n)
            results, seq_holder = [], [0]
            lock = threading.Lock()
            with ThreadPoolExecutor(max_workers=len(workers)) as ex:
                futures = [
                    ex.submit(_worker, wapi, wport, chrome_path, sleep_sec, sli,
                              results, seq_holder, lock, on_result, wi, stop_flag)
                    for wi, ((wapi, wport), sli) in enumerate(zip(workers, slices))
                ]
                for f in futures:
                    f.result()
        finally:
            pool.stop()
        order = {n: i for i, n in enumerate(nodes)}
        results = sorted(results, key=lambda r: order.get(r["node"], 10 ** 9))
    return results

"""ChatGPT 降智检测器 —— 检测 Clash 全部节点的降智情况，并生成顶级规则。

用法：
  python3 main.py                          # 全节点检测
  python3 main.py --nodes "美国-洛杉矶,法国-巴黎"   # 只测指定节点
  python3 main.py --verge                  # 检测后把干净节点写入 Clash Verge 扩展
  python3 main.py --config config.yaml     # 指定配置

流程：global 模式 → 逐节点切换 → headless Chrome 未登录问「你是什么模型」
      → LUNA=干净 / MINI=半干净 / LOGIN_WALL=降智 → 恢复环境 → 生成规则。
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from clash_api import ClashAPI            # noqa: E402
from cdp_tester import HeadlessChrome, test_node, Verdict  # noqa: E402
from rules import generate_rules_yaml, write_verge_extensions  # noqa: E402
from scan import scan_nodes              # noqa: E402

COLOR = {
    "LUNA": "\033[32m",        # 绿
    "MINI": "\033[33m",        # 黄
    "LOGIN_WALL": "\033[31m",  # 红
    "ERROR": "\033[90m",       # 灰
    "RESET": "\033[0m",
}


def load_config(path: str | None) -> dict:
    if not path:
        return {}
    import yaml
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def main():
    ap = argparse.ArgumentParser(description="ChatGPT 降智检测器")
    ap.add_argument("--config", help="YAML 配置文件")
    ap.add_argument("--nodes", help="只测指定节点（逗号分隔），默认全部真实节点")
    ap.add_argument("--chrome", help="Chrome 可执行文件路径")
    ap.add_argument("--proxy-port", type=int, default=7897, help="本机代理端口（Clash 混合端口）")
    ap.add_argument("--verge", action="store_true", help="检测后把干净节点写入 Clash Verge Rev 扩展")
    ap.add_argument("--out", default="./results", help="结果输出目录")
    ap.add_argument("--sleep", type=float, default=3.0, help="切节点后等待秒数")
    ap.add_argument("--reply-timeout", type=float, default=120, help="回复等待秒数（页面慢时自动顺延）")
    ap.add_argument("--parallel", type=int, default=1, help="并行实例数（1=串行；2-4 起临时 mihomo 实例并行检测）")
    ap.add_argument("--no-restore", action="store_true", help="测完不恢复 Clash 环境")
    ap.add_argument("--skip-errors", action="store_true", help="ERROR 节点不进结果表（--out json 时仍保留）")
    args = ap.parse_args()
    cfg = load_config(args.config)

    # 合并：命令行参数优先于配置文件
    proxy_port = args.proxy_port
    sleep_sec = args.sleep

    # ---------- 连接 Clash / 获取节点列表 ----------
    parallel = max(1, args.parallel)
    api_cfg = cfg.get("clash-api", {})
    skip_types = set(cfg.get("skip-types", []))
    if parallel > 1:
        # 并行模式：节点列表也从临时实例取，全程不依赖/不触碰主实例
        print("[1/5] 并行模式：临时 mihomo 实例（不触碰主 Clash）...")
        from mihomo_pool import MihomoPool
        pool_probe = MihomoPool(1)
        inst = pool_probe.start()[0]
        api = ClashAPI(host="127.0.0.1", port=inst.ctl_port)
        print(f"      实例 OK（mixed-port={inst.mixed_port}, ctl={inst.ctl_port}）")
        if args.nodes:
            nodes = [n.strip() for n in args.nodes.split(",") if n.strip()]
        else:
            nodes = [n for n, t in api.list_real_nodes(with_type=True) if t not in skip_types]
        pool_probe.stop()
        print(f"[2/5] 待测节点 {len(nodes)} 个")
        mode_before = global_before = None
    else:
        print("[1/5] 连接 Clash/mihomo 控制端 ...")
        api = ClashAPI(
            socket_path=api_cfg.get("socket") or None,
            host=api_cfg.get("host") or None,
            port=api_cfg.get("port") or None,
        )
        mode_before = api.get_mode()
        global_before = api.get_global_now()
        print(f"      控制端 OK（当前模式={mode_before}, GLOBAL={global_before}）")
        if args.nodes:
            nodes = [n.strip() for n in args.nodes.split(",") if n.strip()]
        else:
            nodes = [n for n, t in api.list_real_nodes(with_type=True) if t not in skip_types]
        print(f"[2/5] 待测节点 {len(nodes)} 个")

    # ---------- 切 global（仅串行模式需要） ----------
    if parallel > 1:
        print(f"[3/5] 并行检测（{parallel} 个出口，临时实例端口 7898+）...")
    else:
        print("[3/5] 切换 global 模式，启动 headless Chrome...")
    if parallel == 1 and not args.no_restore:
        api.set_mode("global")

    os.makedirs(args.out, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    tsv_path = os.path.join(args.out, f"verdicts-{ts}.tsv")
    json_path = os.path.join(args.out, f"verdicts-{ts}.json")

    def on_result(node, r, worker_id, seq):
        v = r["verdict"]
        c_ = COLOR.get(v, "")
        tag = f"W{worker_id + 1}" if parallel > 1 else ""
        print(f"  [{seq}/{len(nodes)}] {c_}{v:<10}{COLOR['RESET']} {tag} {node}  {r['reply'][:60]}")

    results = scan_nodes(api, proxy_port, nodes, parallel=parallel,
                         chrome_path=args.chrome, sleep_sec=sleep_sec,
                         timeout_reply=args.reply_timeout,
                         on_result=on_result)

    # ---------- 恢复环境（仅串行模式动过主实例） ----------
    if parallel == 1 and not args.no_restore:
        try:
            api.set_mode(mode_before)
            if global_before and global_before != "DIRECT":
                api.switch_global(global_before)
        except Exception as e:
            print(f"  ! 环境恢复警告: {e}")
    if parallel == 1:
        print(f"[4/5] 环境已恢复（mode={mode_before}）")

    # ---------- 落盘 + 规则 ----------
    with open(tsv_path, "w", encoding="utf-8") as f:
        f.write("node\tverdict\treply\n")
        for r in results:
            f.write(f"{r['node']}\t{r['verdict']}\t{r['reply']}\n")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    clean = [r["node"] for r in results if r["verdict"] == Verdict.LUNA]
    mini = [r["node"] for r in results if r["verdict"] == Verdict.MINI]
    wall = [r["node"] for r in results if r["verdict"] == Verdict.LOGIN_WALL]
    print(f"[5/5] 完成: 干净 {len(clean)} | 半干净 {len(mini)} | 降智 {len(wall)} | 异常 {len(results)-len(clean)-len(mini)-len(wall)}")
    print(f"      结果: {tsv_path}")

    if not clean:
        print("! 没有检测到干净节点——建议：换机场/换 IP 池后重试，或检查浏览器时区指纹")
        return

    rules_yaml = generate_rules_yaml(clean)
    rules_path = os.path.join(args.out, f"chatgpt-clean-rules-{ts}.yaml")
    with open(rules_path, "w", encoding="utf-8") as f:
        f.write(rules_yaml)
    print(f"      规则片段: {rules_path}")
    print("\n" + rules_yaml)

    if args.verge:
        try:
            info = write_verge_extensions(clean)
            print(f"      Clash Verge 扩展已写入: {info['note']}")
            print(f"        groups: {info['groups_extension']}")
            print(f"        rules : {info['rules_extension']}")
            print("      ! 请到 Clash Verge 订阅页点「重新激活订阅」生效")
        except Exception as e:
            print(f"      ! Verge 扩展写入失败: {e}")


if __name__ == "__main__":
    main()

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
    ap.add_argument("--no-restore", action="store_true", help="测完不恢复 Clash 环境")
    ap.add_argument("--skip-errors", action="store_true", help="ERROR 节点不进结果表（--out json 时仍保留）")
    args = ap.parse_args()
    cfg = load_config(args.config)

    # 合并：命令行参数优先于配置文件
    proxy_port = args.proxy_port
    sleep_sec = args.sleep

    # ---------- 连接 Clash ----------
    api_cfg = cfg.get("clash-api", {})
    print("[1/5] 连接 Clash/mihomo 控制端 ...")
    api = ClashAPI(
        socket_path=api_cfg.get("socket") or None,
        host=api_cfg.get("host") or None,
        port=api_cfg.get("port") or None,
    )
    mode_before = api.get_mode()
    global_before = api.get_global_now()
    print(f"      控制端 OK（当前模式={mode_before}, GLOBAL={global_before}）")

    # ---------- 节点列表 ----------
    if args.nodes:
        nodes = [n.strip() for n in args.nodes.split(",") if n.strip()]
    else:
        skip_types = set(cfg.get("skip-types", []))
        nodes = [n for n, t in api.list_real_nodes(with_type=True) if t not in skip_types]
    print(f"[2/5] 待测节点 {len(nodes)} 个")

    # ---------- 切 global ----------
    print("[3/5] 切换 global 模式，启动 headless Chrome...")
    if not args.no_restore:
        api.set_mode("global")

    os.makedirs(args.out, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    tsv_path = os.path.join(args.out, f"verdicts-{ts}.tsv")
    json_path = os.path.join(args.out, f"verdicts-{ts}.json")
    results = []

    with HeadlessChrome(proxy_port=proxy_port, chrome_path=args.chrome) as chrome:
        for i, node in enumerate(nodes, 1):
            ok = api.switch_global(node)
            if not ok:
                print(f"  [{i}/{len(nodes)}] 切换失败: {node}")
                results.append({"node": node, "verdict": "ERROR", "reply": "switch failed"})
                continue
            time.sleep(sleep_sec)
            r = test_node(chrome, node)
            results.append(r)
            v = r["verdict"]
            c_ = COLOR.get(v, "")
            print(f"  [{i}/{len(nodes)}] {c_}{v:<10}{COLOR['RESET']} {node}  {r['reply'][:60]}")

    # ---------- 恢复环境 ----------
    if not args.no_restore:
        try:
            api.set_mode(mode_before)
            if global_before and global_before != "DIRECT":
                api.switch_global(global_before)
        except Exception as e:
            print(f"  ! 环境恢复警告: {e}")
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
            print("      ⚠️ 请到 Clash Verge 订阅页点「重新激活订阅」生效")
        except Exception as e:
            print(f"      ! Verge 扩展写入失败: {e}")


if __name__ == "__main__":
    main()

"""规则生成器：把「干净节点」写入顶级规则，让 ChatGPT 流量只走它们。

输出两种形式：
1. **通用 mihomo/Clash 配置片段**（默认，适用于任意 Clash 系客户端）：
   - proxy-groups: 新增 ChatGPT-LUNA Selector 组（干净节点为成员）
   - rules: 4 条 OpenAI 域名 → ChatGPT-LUNA（放在规则顶部，优先于订阅自带规则）
2. **Clash Verge Rev 扩展自动写入**（--verge 模式，macOS 实测）：
   自动探测 profiles 目录，把组写入当前订阅链的 groups 扩展、规则写入 rules 扩展。

注意：Clash Verge 的 proxies 扩展只接受真实代理节点定义（放组会报
「unsupport proxy type: select」导致激活失败）——组定义必须放 groups 扩展。
"""
import json
import os
from pathlib import Path

GROUP_NAME = "ChatGPT-LUNA"

# OpenAI 相关域名（降智判定针对的流量）
OPENAI_RULES = [
    ("DOMAIN-SUFFIX", "openai.com"),
    ("DOMAIN-SUFFIX", "chatgpt.com"),
    ("DOMAIN-SUFFIX", "oaiusercontent.com"),
    ("DOMAIN-SUFFIX", "oaistatic.com"),
]

# Clash Verge Rev 数据目录（各平台）
VERGE_DIRS = [
    "~/Library/Application Support/io.github.clash-verge-rev.clash-verge-rev/profiles",  # macOS
    "~/.config/clash-verge-rev/profiles",    # Linux
    "%APPDATA%/io.github.clash-verge-rev.clash-verge-rev/profiles",  # Windows
]


def generate_rules_yaml(clean_nodes: list, group_name: str = GROUP_NAME) -> str:
    """生成 mihomo 配置片段（proxy-groups + rules），可直接粘贴。"""
    lines = []
    lines.append("# ChatGPT 降智检测生成 —— 干净节点规则片段")
    lines.append(f"# 生成时间: {__import__('datetime').datetime.now().isoformat(timespec='seconds')}")
    lines.append("# 用法: 把 proxy-groups 段合并进你的 proxy-groups，")
    lines.append("#       把 rules 段（DOMAIN-SUFFIX 4 条）放到 rules 列表最顶部")
    lines.append("")
    lines.append("proxy-groups:")
    lines.append(f"  - name: {group_name}")
    lines.append("    type: select")
    lines.append("    proxies:")
    for n in clean_nodes:
        lines.append(f"      - {n}")
    lines.append("")
    lines.append("rules:")
    for kind, dom in OPENAI_RULES:
        lines.append(f"  - {kind},{dom},{group_name}")
    lines.append("")
    return "\n".join(lines)


def find_verge_profiles_dir() -> Path | None:
    for d in VERGE_DIRS:
        p = Path(os.path.expandvars(os.path.expanduser(d)))
        if p.is_dir():
            return p
    return None


def _load_profiles_yaml(profiles_dir: Path):
    import yaml
    with open(profiles_dir / "profiles.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def write_verge_extensions(clean_nodes: list, group_name: str = GROUP_NAME,
                           profiles_dir: Path | None = None) -> dict:
    """自动写入 Clash Verge Rev 扩展（当前激活订阅链的 rules/groups 扩展）。

    要求：激活的订阅链已有 rules 与 groups 扩展文件（uid 记录在 profiles.yaml）。
    写入后需在 Clash Verge 订阅页点「重新激活订阅」生效。
    """
    import yaml
    profiles_dir = profiles_dir or find_verge_profiles_dir()
    if not profiles_dir:
        raise RuntimeError("未找到 Clash Verge profiles 目录")

    data = _load_profiles_yaml(profiles_dir)
    current = data.get("current")
    items = {it["uid"]: it for it in data.get("items", []) if it.get("uid")}
    chain = []
    uid = current
    seen = set()
    while uid and uid not in seen:
        seen.add(uid)
        it = items.get(uid)
        if not it:
            break
        chain.append(it)
        uid = it.get("chain")
    if not chain:
        raise RuntimeError("无法解析当前订阅链")

    groups_uid = next((it["uid"] for it in chain if it.get("type") == "groups"), None)
    rules_uid = next((it["uid"] for it in chain if it.get("type") == "rules"), None)
    if not groups_uid or not rules_uid:
        raise RuntimeError("当前订阅链缺少 groups/rules 扩展，请先在 Clash Verge 订阅页添加")

    # groups 扩展：append 段放 ChatGPT-LUNA 组
    gp_path = profiles_dir / f"{groups_uid}.yaml"
    gp = yaml.safe_load(gp_path.read_text(encoding="utf-8")) or {}
    append = gp.setdefault("append", [])
    # 移除旧组，避免重复
    append = [g for g in append if not (isinstance(g, dict) and g.get("name") == group_name)]
    append.append({"name": group_name, "type": "select", "proxies": list(clean_nodes)})
    gp["append"] = append
    gp_path.write_text(yaml.safe_dump(gp, allow_unicode=True, sort_keys=False), encoding="utf-8")

    # rules 扩展：prepend 段放 4 条 OpenAI 规则
    rp_path = profiles_dir / f"{rules_uid}.yaml"
    rp = yaml.safe_load(rp_path.read_text(encoding="utf-8")) or {}
    prepend = rp.setdefault("prepend", [])
    for kind, dom in OPENAI_RULES:
        rule = f"{kind},{dom},{group_name}"
        if rule in prepend:
            continue
        prepend.insert(0, rule)
    rp["prepend"] = prepend
    rp_path.write_text(yaml.safe_dump(rp, allow_unicode=True, sort_keys=False), encoding="utf-8")

    return {
        "groups_extension": str(gp_path),
        "rules_extension": str(rp_path),
        "note": "已写入扩展文件，请在 Clash Verge 订阅页点「重新激活订阅」生效",
    }


if __name__ == "__main__":
    # 自测
    demo = ["韩国KR-HY2", "台湾-优化", "法国FR-A"]
    print(generate_rules_yaml(demo))

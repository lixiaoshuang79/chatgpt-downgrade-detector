"""临时 mihomo 实例池：并行检测的核心。

原理：Clash/mihomo 同一时刻只有一个出口（GLOBAL 只能选一个节点），
逐节点串行切换是单实例检测慢的根因。本模块从 Clash Verge 的合并运行
配置（clash-verge.yaml，含全部节点/组/扩展）复制出 N 份临时配置，
各自起一个独立 mihomo 实例（不同 mixed-port / external-controller），
每个实例独占一个出口，即可并行检测多批节点。

临时实例只监听 127.0.0.1、用完即杀，不影响正在运行的 Clash Verge。
"""
import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.request

# macOS Clash Verge Rev / Linux 常见 mihomo 路径
MIHOMO_PATHS = [
    "/Applications/Clash Verge.app/Contents/MacOS/verge-mihomo",
    "/Applications/Clash Verge.app/Contents/MacOS/mihomo",
    "/usr/local/bin/mihomo",
    "/usr/bin/mihomo",
]
# Clash Verge Rev 数据目录（含运行时合并配置 clash-verge.yaml 与 geo 数据）
VERGE_DIRS = [
    os.path.expanduser("~/Library/Application Support/io.github.clash-verge-rev.clash-verge-rev"),
    os.path.expanduser("~/.config/clash-verge-rev"),
    os.path.expanduser("~/.config/clash-verge"),
]
GEO_FILES = ("Country.mmdb", "geoip.dat", "geosite.dat")


def find_mihomo() -> str:
    for p in MIHOMO_PATHS:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    raise RuntimeError("未找到 mihomo 可执行文件（本机为 Clash Verge 内置 verge-mihomo）")


def find_base_config() -> tuple[str, str]:
    """返回 (合并配置路径, 数据目录)。优先 Clash Verge 运行时配置。"""
    for d in VERGE_DIRS:
        cfg = os.path.join(d, "clash-verge.yaml")
        if os.path.isfile(cfg):
            return cfg, d
    raise RuntimeError("未找到 Clash Verge 合并配置（clash-verge.yaml）")


class ManagedMihomo:
    """单个临时 mihomo 实例。"""

    def __init__(self, index: int, base_cfg: str, base_dir: str,
                 mixed_port: int, ctl_port: int):
        self.index = index
        self.mixed_port = mixed_port
        self.ctl_port = ctl_port
        self.tmpdir = tempfile.mkdtemp(prefix=f"cdd-mihomo-{index}-")
        self._proc = None
        self._build_config(base_cfg, base_dir)

    def _build_config(self, base_cfg: str, base_dir: str):
        import yaml
        with open(base_cfg, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        # 测试实例：独立端口 + global 模式 + 仅本机监听 + 无鉴权（仅 127.0.0.1）
        cfg["mixed-port"] = self.mixed_port
        cfg["port"] = 0
        cfg["socks-port"] = 0
        cfg["redir-port"] = 0
        cfg["external-controller"] = f"127.0.0.1:{self.ctl_port}"
        cfg.pop("secret", None)
        # 关键：必须移除 unix 控制端配置——主实例用它（/tmp/verge/verge-mihomo.sock），
        # 临时实例若沿用会抢占/覆盖同一个 socket 文件，导致主 Clash 控制端失效
        cfg.pop("external-controller-unix", None)
        cfg["mode"] = "global"
        cfg["allow-lan"] = False
        cfg.pop("external-ui", None)
        cfg.pop("external-controller-tls", None)
        self.cfg_path = os.path.join(self.tmpdir, "config.yaml")
        with open(self.cfg_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
        # geo 数据软链（规则里的 GEOIP/GEOSITE 需要）
        for g in GEO_FILES:
            src = os.path.join(base_dir, g)
            if os.path.isfile(src):
                try:
                    os.symlink(src, os.path.join(self.tmpdir, g))
                except FileExistsError:
                    pass

    def start(self, binary: str, timeout: float = 15.0):
        log = open(os.path.join(self.tmpdir, "mihomo.log"), "w")
        self._proc = subprocess.Popen(
            [binary, "-d", self.tmpdir, "-f", self.cfg_path],
            stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        )
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._proc.poll() is not None:
                raise RuntimeError(f"mihomo 实例 {self.index} 启动失败（exit {self._proc.returncode}），"
                                   f"日志见 {os.path.join(self.tmpdir, 'mihomo.log')}")
            if self._ping():
                return
            time.sleep(0.5)
        raise RuntimeError(f"mihomo 实例 {self.index} 就绪超时")

    def _ping(self) -> bool:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.ctl_port}/version", timeout=2) as r:
                return r.status == 200
        except Exception:
            return False

    def stop(self):
        if self._proc:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=5)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.stop()


class MihomoPool:
    """管理 N 个临时实例（不含正在运行的主实例）。"""

    def __init__(self, count: int, base_mixed_port: int = 7898,
                 base_ctl_port: int = 9091, binary: str | None = None):
        self.count = count
        self.binary = binary or find_mihomo()
        self.base_cfg, self.base_dir = find_base_config()
        self.base_mixed_port = base_mixed_port
        self.base_ctl_port = base_ctl_port
        self.instances: list[ManagedMihomo] = []

    def start(self) -> list[ManagedMihomo]:
        """启动全部实例（索引 1..N，索引 0 是主实例）。"""
        for i in range(1, self.count + 1):
            inst = ManagedMihomo(
                i, self.base_cfg, self.base_dir,
                mixed_port=self.base_mixed_port + (i - 1),
                ctl_port=self.base_ctl_port + (i - 1),
            )
            inst.start(self.binary)
            self.instances.append(inst)
        return self.instances

    def stop(self):
        for inst in self.instances:
            try:
                inst.stop()
            except Exception:
                pass
        self.instances = []

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()


if __name__ == "__main__":
    import sys
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    print(f"启动 {n} 个临时实例（端口 {7898}+ / 控制端 {9091}+）...")
    with MihomoPool(n) as pool:
        for inst in pool:
            print(f"  实例{inst.index}: mixed-port={inst.mixed_port} ctl={inst.ctl_port} OK")
        time.sleep(3)
    print("已全部停止")

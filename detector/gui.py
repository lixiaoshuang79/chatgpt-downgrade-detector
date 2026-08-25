#!/usr/bin/env python3
"""ChatGPT 降智检测器 —— 原生桌面 GUI（PySide6 / Qt，深色主题）。

用法:
  pip install PySide6
  python3 detector/gui.py

功能:
  - 连接本地 Clash/mihomo 控制端，列出全部真实节点
  - 勾选节点 → 检测（headless Chrome 判定 LUNA/MINI/LOGIN_WALL）
  - 检测完成后生成顶级规则，可复制或写入 Clash Verge 扩展
  - 浏览器插件（可选）：显示 zip 路径，打开所在目录
"""
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from PySide6.QtCore import Qt, QTimer, Signal, QObject
from PySide6.QtGui import QColor, QFont, QPainter, QBrush, QPen, QAction
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QPushButton, QCheckBox, QFrame, QScrollArea, QPlainTextEdit,
    QMessageBox, QSizePolicy,
)

from clash_api import ClashAPI
from cdp_tester import HeadlessChrome, test_node, Verdict
from rules import generate_rules_yaml, write_verge_extensions

# ---------- 主题色 ----------
BG = "#0d1220"
BG2 = "#101828"
CARD = "#151d33"
CARD2 = "#1a2440"
BORDER = "#26304f"
TEXT = "#e8ebf5"
DIM = "#8b93ab"
ACCENT = "#7c6cff"
ACCENT2 = "#4f9dff"
GREEN = "#34d399"
YELLOW = "#fbbf24"
RED = "#f87171"
GRAY = "#64748b"

QSS = f"""
QMainWindow, QWidget {{ background: {BG}; color: {TEXT}; font-family: "PingFang SC"; }}
QFrame#card, QFrame[card="true"] {{
    background: {CARD}; border: 1px solid {BORDER}; border-radius: 14px;
}}
QLabel#h1 {{ font-size: 18px; font-weight: 700; }}
QLabel#h2 {{ font-size: 13px; font-weight: 700; color: {TEXT}; }}
QLabel#dim {{ font-size: 11px; color: {DIM}; }}
QLabel#mono {{ font-family: "Menlo", Menlo, monospace; font-size: 11px; color: {DIM}; }}

QPushButton {{
    background: {CARD2}; color: {TEXT}; border: 1px solid {BORDER}; border-radius: 10px;
    padding: 9px 16px; font-size: 13px; font-weight: 600;
}}
QPushButton:hover {{ background: #202b4d; border-color: #33406e; }}
QPushButton:pressed {{ background: #182138; }}
QPushButton:disabled {{ color: #55607d; background: {CARD}; }}
QPushButton#primary {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 {ACCENT}, stop:1 {ACCENT2});
    border: none; color: white; font-size: 14px; font-weight: 700;
}}
QPushButton#primary:hover {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #8f81ff, stop:1 #5fa8ff); }}
QPushButton#primary:disabled {{ background: #3a3f5c; color: #8b93ab; }}
QPushButton#danger {{ color: {RED}; border-color: rgba(248,113,113,0.35); }}
QPushButton#chip {{
    padding: 4px 12px; border-radius: 999px; font-size: 11px;
    background: rgba(255,255,255,0.05); border: 1px solid {BORDER}; color: {DIM};
}}
QPushButton#chip[active="true"] {{ background: rgba(124,108,255,0.22); border-color: {ACCENT}; color: {TEXT}; }}
QPushButton#selall {{ padding: 4px 12px; border-radius: 999px; font-size: 11px; }}

QScrollArea {{ border: none; background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 8px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: #2c3552; border-radius: 4px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: #3a4568; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}

QPlainTextEdit {{ background: #0a0e18; color: #c9d2e8; border: 1px solid {BORDER};
    border-radius: 10px; font-family: "Menlo", Menlo, monospace; font-size: 11px; padding: 10px; }}
QCheckBox {{ spacing: 8px; }}
QCheckBox::indicator {{ width: 16px; height: 16px; border-radius: 5px;
    border: 1.5px solid #3a4568; background: transparent; }}
QCheckBox::indicator:checked {{ background: qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 {ACCENT},stop:1 {ACCENT2});
    border-color: transparent; }}
QCheckBox::indicator:hover {{ border-color: {ACCENT}; }}
QToolTip {{ background: #1a2440; color: {TEXT}; border: 1px solid {BORDER}; padding: 6px; }}
"""

VERDICT_STYLE = {
    Verdict.LUNA: (GREEN, "LUNA"),
    Verdict.MINI: (YELLOW, "MINI"),
    Verdict.LOGIN_WALL: (RED, "LOGIN_WALL"),
    Verdict.ERROR: (GRAY, "ERROR"),
}
STAT_KEYS = [("clean", GREEN, "🟢", "干净 LUNA"), ("mini", YELLOW, "🟡", "半干净 MINI"),
             ("wall", RED, "🔴", "降智 LOGIN_WALL"), ("err", GRAY, "⚪", "异常 ERROR")]


class ScanState:
    def __init__(self):
        self.running = False
        self.stop_requested = False
        self.current = None
        self.total = 0
        self.done = 0
        self.results = []
        self.error = None

    def reset(self):
        self.running = False
        self.stop_requested = False
        self.current = None
        self.total = 0
        self.done = 0
        self.results = []
        self.error = None


class Dot(QLabel):
    """状态圆点。"""

    def __init__(self, color="#64748b"):
        super().__init__()
        self._color = QColor(color)
        self.setFixedSize(10, 10)

    def set_color(self, color):
        self._color = QColor(color)
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(self._color))
        p.drawEllipse(1, 1, 8, 8)
        if self._color.alpha() > 0:
            glow = QColor(self._color)
            glow.setAlpha(60)
            p.setBrush(QBrush(glow))
            p.drawEllipse(0, 0, 10, 10)


class NodeRow(QFrame):
    """节点行：勾选 + 状态点 + 名称 + 判定标签。"""

    def __init__(self, name, parent=None):
        super().__init__(parent)
        self.name = name
        self.setObjectName("card")
        self.setStyleSheet(
            "QFrame#card { background: #1a2340; border: 1px solid #232d4d; border-radius: 10px; }"
            "QFrame#card:hover { background: #1f2a4d; }"
        )
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 7, 10, 7)
        lay.setSpacing(10)
        self.ck = QCheckBox()
        lay.addWidget(self.ck)
        self.dot = Dot()
        lay.addWidget(self.dot)
        self.name_label = QLabel(name)
        self.name_label.setStyleSheet("background: transparent; font-size: 12.5px; color: #e8ebf5;")
        self.name_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        lay.addWidget(self.name_label, 1)
        self.tag = QLabel("待测")
        self.tag.setStyleSheet("background: transparent; color: #64748b; font-size: 10px; font-weight: 700;")
        lay.addWidget(self.tag)

    def set_verdict(self, verdict):
        color, text = VERDICT_STYLE.get(verdict, (GRAY, "ERROR"))
        self.dot.set_color(color)
        self.tag.setText(text)
        self.tag.setStyleSheet(f"background: transparent; color: {color}; font-size: 10px; font-weight: 700;")


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("ChatGPT 降智检测器")
        self.resize(1040, 740)
        self.setMinimumSize(880, 640)

        self.api = None
        self.nodes = []
        self.selected = set()
        self.results = {}
        self.filter = ""
        self.state = ScanState()
        self.rows = {}          # name -> NodeRow
        self.chip_btns = {}

        self._build_ui()
        self._connect()
        self.poll_timer = QTimer(self)
        self.poll_timer.timeout.connect(self._poll)
        self.poll_timer.start(500)

    # ================= UI =================
    def _card(self):
        f = QFrame()
        f.setObjectName("card")
        return f

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(20, 16, 20, 14)
        root.setSpacing(12)

        # --- 顶栏 ---
        head = QHBoxLayout()
        title = QLabel("🛡  ChatGPT 降智检测器")
        title.setObjectName("h1")
        head.addWidget(title)
        head.addStretch(1)
        self.badge = QLabel("● 连接中…")
        self.badge.setObjectName("dim")
        self.badge.setStyleSheet(
            f"background: {CARD}; border: 1px solid {BORDER}; border-radius: 999px;"
            "padding: 6px 14px; font-size: 11px; color: #8b93ab;")
        head.addWidget(self.badge)
        root.addLayout(head)

        body = QHBoxLayout()
        body.setSpacing(14)
        root.addLayout(body, 1)

        # --- 左栏 ---
        left = QVBoxLayout()
        left.setSpacing(12)
        body.addLayout(left, 0)

        # 统计卡
        stats_card = self._card()
        grid = QGridLayout(stats_card)
        grid.setContentsMargins(12, 12, 12, 12)
        grid.setSpacing(8)
        self.stat_labels = {}
        for i, (key, color, ico, lbl) in enumerate(STAT_KEYS):
            box = QFrame()
            box.setStyleSheet(
                f"background: {CARD2}; border-radius: 10px;"
                f"border-left: 3px solid {color};")
            blay = QHBoxLayout(box)
            blay.setContentsMargins(10, 8, 10, 8)
            ico_l = QLabel(ico)
            ico_l.setStyleSheet("background: transparent; font-size: 15px;")
            blay.addWidget(ico_l)
            col = QVBoxLayout()
            col.setSpacing(0)
            num = QLabel("0")
            num.setStyleSheet(f"background: transparent; color: {color}; font-size: 19px; font-weight: 800;")
            lab = QLabel(lbl)
            lab.setStyleSheet(f"background: transparent; color: {DIM}; font-size: 9.5px;")
            col.addWidget(num)
            col.addWidget(lab)
            blay.addLayout(col)
            grid.addWidget(box, i // 2, i % 2)
            self.stat_labels[key] = num
        left.addWidget(stats_card)

        # 进度
        prog_card = self._card()
        play = QVBoxLayout(prog_card)
        play.setContentsMargins(14, 12, 14, 12)
        self.prog_cur = QLabel("等待开始")
        self.prog_cur.setObjectName("dim")
        play.addWidget(self.prog_cur)
        self.prog_pct = QLabel("0%")
        self.prog_pct.setStyleSheet(
            f"background: transparent; color: {ACCENT2}; font-size: 13px; font-weight: 800;")
        play.addWidget(self.prog_pct)
        self.prog_bar = QFrame()
        self.prog_bar.setFixedHeight(8)
        self.prog_bar.setStyleSheet(
            f"background: #0a0e18; border: none; border-radius: 4px;")
        play.addWidget(self.prog_bar)
        self.prog_fill = QLabel()
        self.prog_fill.setFixedHeight(8)
        self.prog_fill.setStyleSheet(
            f"background: qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 {ACCENT},stop:1 {ACCENT2});"
            "border: none; border-radius: 4px;")
        play.addWidget(self.prog_fill)
        self.prog_holder = QFrame()
        self.prog_holder.setFixedHeight(8)
        hlay = QHBoxLayout(self.prog_holder)
        hlay.setContentsMargins(0, 0, 0, 0)
        hlay.addWidget(self.prog_fill)
        left.addWidget(prog_card)

        # 按钮
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        self.btn_scan = QPushButton("▶  开始检测")
        self.btn_scan.setObjectName("primary")
        self.btn_scan.clicked.connect(self._start_scan)
        self.btn_stop = QPushButton("■  停止")
        self.btn_stop.setObjectName("danger")
        self.btn_stop.clicked.connect(self._stop_scan)
        self.btn_stop.setEnabled(False)
        btn_row.addWidget(self.btn_scan, 1)
        btn_row.addWidget(self.btn_stop, 1)
        left.addLayout(btn_row)

        rule_row = QHBoxLayout()
        rule_row.setSpacing(8)
        self.btn_apply = QPushButton("⚙  写入 Clash Verge 扩展")
        self.btn_apply.clicked.connect(self._apply_rules)
        self.btn_apply.setEnabled(False)
        self.btn_copy = QPushButton("📋  复制规则")
        self.btn_copy.clicked.connect(self._copy_rules)
        self.btn_copy.setEnabled(False)
        rule_row.addWidget(self.btn_apply, 1)
        rule_row.addWidget(self.btn_copy, 1)
        left.addLayout(rule_row)

        # 环境信息
        env_card = self._card()
        elay = QVBoxLayout(env_card)
        elay.setContentsMargins(14, 10, 14, 10)
        self.env_text = QLabel("控制端: —\n模式: —\nGLOBAL: —\n节点总数: —")
        self.env_text.setObjectName("mono")
        self.env_text.setStyleSheet(
            f"font-family: 'Menlo', Menlo, monospace; font-size: 10.5px; color: {DIM};"
            "background: transparent; line-height: 1.7;")
        elay.addWidget(self.env_text)
        left.addWidget(env_card)

        # 插件
        plug_card = self._card()
        play2 = QVBoxLayout(plug_card)
        play2.setContentsMargins(14, 10, 14, 10)
        p1 = QLabel("🌐 浏览器插件（可选）")
        p1.setObjectName("h2")
        play2.addWidget(p1)
        p2 = QLabel("指纹核验 + 自动改时区，解决时区不匹配降智")
        p2.setObjectName("dim")
        play2.addWidget(p2)
        prow = QHBoxLayout()
        self.plug_path = QLabel("—")
        self.plug_path.setObjectName("mono")
        prow.addWidget(self.plug_path, 1)
        self.btn_plug = QPushButton("打开目录")
        self.btn_plug.clicked.connect(self._open_plugin_dir)
        prow.addWidget(self.btn_plug)
        play2.addLayout(prow)
        left.addWidget(plug_card)
        left.addStretch(1)

        # --- 右栏：节点列表 ---
        right_card = self._card()
        rlay = QVBoxLayout(right_card)
        rlay.setContentsMargins(12, 12, 12, 10)
        rlay.setSpacing(8)

        head2 = QHBoxLayout()
        self.sel_label = QLabel("节点列表（已选 0/0）")
        self.sel_label.setObjectName("h2")
        head2.addWidget(self.sel_label)
        head2.addStretch(1)
        self.btn_selall = QPushButton("全选")
        self.btn_selall.setObjectName("selall")
        self.btn_selall.clicked.connect(self._toggle_select_all)
        head2.addWidget(self.btn_selall)
        for cf, cn in [("", "全部"), ("LUNA", "干净"), ("MINI", "半干净"),
                       ("LOGIN_WALL", "降智"), ("ERROR", "异常")]:
            b = QPushButton(cn)
            b.setObjectName("chip")
            b.setProperty("active", cf == "")
            b.clicked.connect(lambda checked=False, f=cf: self._set_filter(f))
            head2.addWidget(b)
            self.chip_btns[cf] = b
        rlay.addLayout(head2)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self.list_host = QWidget()
        self.list_host.setStyleSheet(f"background: transparent;")
        self.list_lay = QVBoxLayout(self.list_host)
        self.list_lay.setContentsMargins(0, 0, 4, 0)
        self.list_lay.setSpacing(6)
        self.list_lay.addStretch(1)
        scroll.setWidget(self.list_host)
        rlay.addWidget(scroll, 1)
        body.addWidget(right_card, 1)

        # --- 底部规则预览 ---
        rp = QLabel("📐 顶级规则预览")
        rp.setObjectName("h2")
        root.addWidget(rp)
        self.rules_text = QPlainTextEdit()
        self.rules_text.setReadOnly(True)
        self.rules_text.setFixedHeight(130)
        root.addWidget(self.rules_text)

    # ================= 数据 =================
    def _connect(self):
        try:
            self.api = ClashAPI()
            self.nodes = self.api.list_real_nodes()
            self.selected = set(self.nodes)
        except Exception as e:
            self._set_badge(False, str(e)[:40])
            return
        self._set_badge(True, f"Clash 已连接 · {len(self.nodes)} 节点")
        self._refresh_env()
        self._rebuild_rows()

    def _set_badge(self, ok, text):
        color = GREEN if ok else RED
        self.badge.setStyleSheet(
            f"background: {CARD}; border: 1px solid {BORDER}; border-radius: 999px;"
            f"padding: 6px 14px; font-size: 11px; color: {color};")
        self.badge.setText(f"● {text}")

    def _refresh_env(self):
        try:
            mode = self.api.get_mode()
            gnow = self.api.get_global_now()
            self.env_text.setText(f"控制端: {self.api._endpoint}\n模式: {mode}\n"
                                  f"GLOBAL: {gnow}\n节点总数: {len(self.nodes)}")
        except Exception:
            pass

    # ================= 节点列表 =================
    def _rebuild_rows(self):
        for w in self.rows.values():
            self.list_lay.removeWidget(w)
            w.deleteLater()
        self.rows = {}
        shown = [n for n in self.nodes if not self.filter or
                 (self.results.get(n) and self.results[n]["verdict"] == self.filter)]
        if not shown:
            empty = QLabel("未发现可检测的节点" if not self.filter else "该分类暂无节点")
            empty.setObjectName("dim")
            empty.setAlignment(Qt.AlignCenter)
            self.list_lay.insertWidget(0, empty)
            return
        for i, name in enumerate(shown):
            row = NodeRow(name)
            row.ck.setChecked(name in self.selected)
            row.ck.toggled.connect(lambda on, n=name: self._on_toggle(n, on))
            self.list_lay.insertWidget(i, row)
            self.rows[name] = row
        self._update_sel_label()

    def _on_toggle(self, name, on):
        if on:
            self.selected.add(name)
        else:
            self.selected.discard(name)
        self._update_sel_label()

    def _toggle_select_all(self):
        if len(self.selected) == len(self.nodes):
            self.selected.clear()
        else:
            self.selected = set(self.nodes)
        for name, row in self.rows.items():
            row.ck.setChecked(name in self.selected)
        self._update_sel_label()

    def _set_filter(self, f):
        self.filter = f
        for cf, b in self.chip_btns.items():
            b.setProperty("active", cf == f)
            b.style().unpolish(b)
            b.style().polish(b)
        self._rebuild_rows()

    def _update_sel_label(self):
        self.sel_label.setText(f"节点列表（已选 {len(self.selected)}/{len(self.nodes)}）")
        self.btn_scan.setText(f"▶  开始检测（{len(self.selected)}）")
        all_on = len(self.nodes) > 0 and len(self.selected) == len(self.nodes)
        self.btn_selall.setText("取消全选" if all_on else "全选")

    # ================= 检测 =================
    def _start_scan(self):
        if self.state.running:
            return
        if not self.selected:
            QMessageBox.warning(self, "提示", "请先勾选要检测的节点")
            return
        self.state.reset()
        self.state.total = len(self.selected)
        self.results = {}
        for name, row in self.rows.items():
            row.set_verdict(None)
        self._update_stats()
        self._set_progress(0)
        self.btn_scan.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.btn_apply.setEnabled(False)
        self.btn_copy.setEnabled(False)
        self._set_badge(True, "Clash 已连接 · 检测中")
        threading.Thread(target=self._run_scan, args=(list(self.selected),), daemon=True).start()

    def _run_scan(self, nodes):
        api = self.api
        mode_before = api.get_mode()
        global_before = api.get_global_now()
        self.state.running = True
        try:
            api.set_mode("global")
            with HeadlessChrome() as chrome:
                for i, node in enumerate(nodes):
                    if self.state.stop_requested:
                        break
                    self.state.current = node
                    self.state.done = i
                    ok = api.switch_global(node)
                    if not ok:
                        self.state.results.append({"node": node, "verdict": Verdict.ERROR, "reply": "切换失败"})
                        continue
                    time.sleep(3)
                    r = test_node(chrome, node)
                    self.state.results.append(r)
                self.state.done = len(nodes)
        except Exception as e:
            self.state.error = str(e)
        finally:
            try:
                api.set_mode(mode_before)
                if global_before and global_before != "DIRECT":
                    api.switch_global(global_before)
            except Exception:
                pass
            self.state.running = False
            self.state.current = None

    def _stop_scan(self):
        self.state.stop_requested = True
        self.btn_stop.setEnabled(False)

    def _poll(self):
        s = self.state
        for r in s.results:
            self.results.setdefault(r["node"], r)
        self._update_stats()
        if s.running:
            self._set_progress(s.done / s.total if s.total else 0)
            self.prog_cur.setText(f"正在检测 {s.current}" if s.current else "准备中…")
            self._refresh_rows()
        else:
            self._set_progress(len(s.results) / s.total if s.total else 0)
            self.prog_cur.setText("检测完成 ✓" if s.results else "等待开始")
            self._refresh_rows()
            if s.results or s.error:
                self.btn_scan.setEnabled(True)
                self.btn_stop.setEnabled(False)
                self._update_sel_label()
                self._refresh_env()
                if s.error:
                    self._set_badge(False, "检测异常")
                    QMessageBox.critical(self, "检测异常", s.error[:300])
                else:
                    self._set_badge(True, "Clash 已连接")
                self._show_rules()

    def _refresh_rows(self):
        for name, row in self.rows.items():
            r = self.results.get(name)
            if r:
                row.set_verdict(r["verdict"])

    def _set_progress(self, ratio):
        ratio = max(0.0, min(1.0, ratio))
        self.prog_pct.setText(f"{int(ratio * 100)}%")
        self.prog_fill.setFixedWidth(int(self.prog_holder.width() * ratio))

    def _update_stats(self):
        c = m = w = e = 0
        for r in self.results.values():
            if r["verdict"] == Verdict.LUNA:
                c += 1
            elif r["verdict"] == Verdict.MINI:
                m += 1
            elif r["verdict"] == Verdict.LOGIN_WALL:
                w += 1
            else:
                e += 1
        self.stat_labels["clean"].setText(str(c))
        self.stat_labels["mini"].setText(str(m))
        self.stat_labels["wall"].setText(str(w))
        self.stat_labels["err"].setText(str(e))

    # ================= 规则 =================
    def _show_rules(self):
        clean = [r["node"] for r in self.results.values() if r["verdict"] == Verdict.LUNA]
        self.rules_text.setPlainText(
            generate_rules_yaml(clean) if clean else "# 未检测出干净节点 — 可换机场 / 换 IP 池后重测")
        self.btn_apply.setEnabled(bool(clean))
        self.btn_copy.setEnabled(bool(clean))

    def _copy_rules(self):
        QApplication.clipboard().setText(self.rules_text.toPlainText())
        QMessageBox.information(self, "已复制", "规则片段已复制到剪贴板")

    def _apply_rules(self):
        clean = [r["node"] for r in self.results.values() if r["verdict"] == Verdict.LUNA]
        if not clean:
            return
        try:
            info = write_verge_extensions(clean)
        except Exception as e:
            QMessageBox.critical(self, "写入失败", str(e))
            return
        QMessageBox.information(
            self, "已写入",
            f"已写入 {len(clean)} 个干净节点：\n\n"
            f"groups: {Path(info['groups_extension']).name}\n"
            f"rules : {Path(info['rules_extension']).name}\n\n"
            "请到 Clash Verge 订阅页点「重新激活订阅」生效")

    # ================= 插件 =================
    def _plugin_zip(self):
        ext = Path(__file__).parent.parent / "extension"
        zips = sorted(ext.glob("*.zip")) if ext.is_dir() else []
        return zips[0] if zips else None

    def _open_plugin_dir(self):
        z = self._plugin_zip()
        target = z.parent if z else Path(__file__).parent.parent / "extension"
        try:
            if sys.platform == "darwin":
                subprocess.Popen(["open", str(target)])
            elif os.name == "nt":
                os.startfile(str(target))  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["xdg-open", str(target)])
        except Exception as e:
            QMessageBox.critical(self, "无法打开", str(e))


def main():
    app = QApplication(sys.argv)
    app.setStyleSheet(QSS)
    w = MainWindow()
    w.show()
    zipf = w._plugin_zip()
    w.plug_path.setText(zipf.name if zipf else "未找到 zip（extension/ 目录）")
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

# 浏览器插件安装说明（ChatGPT 指纹核验 + 自动改时区 v3.1.1）

## 适用浏览器
Chrome / Edge / Chromium（Manifest V3）。

## 安装（未打包版）

1. 下载本项目 `extension/` 目录（或 git clone）
2. 打开 `chrome://extensions`
3. 右上角开启「开发者模式」
4. 点「加载已解压的扩展程序」→ 选择 `extension/` 目录
5. 打开 https://chatgpt.com/ ，右上角出现核验条即生效

## 安装（打包版 zip，分发给同事）

1. 解压 `chatgpt-tz-fix-extension-v3.1.1.zip`
2. 同上步骤「加载已解压的扩展程序」（zip 需先解压）

> 给同事的注意事项：打包版未上架 Chrome 商店，加载时浏览器会提示
> 「请停用以开发者模式运行的扩展程序」——这是正常提示，保持开启即可；
> 部分公司策略会禁用开发者模式扩展，此时需走企业策略安装（不在本说明范围）。

## 权限说明

- `debugger`：用于 `Emulation.setTimezoneOverride` 改浏览器时区（进站自动，无需手动）
- `storage`：记住你选择的时区偏好
- 主机权限：`chatgpt.com`（核验/改时区）、`ip-api.com` / `api.ip.sb`（查询出口 IP）
  —— IP 查询请求在扩展后台发起，**不经过页面**，不受 chatgpt.com CSP 限制

## 使用说明

- 进站自动核验：核验条 🟢 = 时区与 IP 匹配；🔴 = 不匹配（已自动修正）
- 顶部出现「xxx 正在调试此浏览器」提示条 = chrome.debugger 正常工作副作用，**不要点取消**（点取消会让时区设置失效）
- 重启浏览器后自动重新附加，无需手动操作

## 常见问题

- 核验条不出现：确认扩展已启用；`chrome://extensions` 里该扩展的「允许访问文件网址」无需开
- IP 查询失败：ip-api.com 免费版仅支持 http（扩展已自动处理）；企业网络拦截时换 api.ip.sb

# Dashboard 仪表盘

[English](./README.md) | [简体中文](./README_zh.md)

NanoKVM Go 的 dashboard，在屏幕和浏览器上都能查看：

- **被控主机**：HDMI 输入（有无信号、分辨率、帧率、H.264 推流、是否有人在看 KVM 页面），以及传输键盘鼠标的 USB 连接（是否连接、速度、模拟的设备）
- **系统**：CPU（总占用、每核、频率）、内存和 swap、磁盘、所有温度传感器、负载、内核版本，以及 NanoKVM 相关服务的运行状态
- **网络**：LAN 和 Tailscale IP、网关、DNS 服务器、Wi-Fi（网络名、信号、频段和信道、连接速率）、Tailscale 状态和在线节点数、网卡、连通性检测，以及 ping / DNS 查询小工具
- **进程**：按 CPU 排序的进程（类似 `top`）
- **设置**：翻页方式、自动息屏、连通性检测的目标，在屏幕和网页上都能改

网页没有访问口令，所以日志和其他 Tailscale 设备的名称、地址都不会显示。

![Dashboard 网页 Overview 标签](../_docs/dashboard/web-overview.png)

## 屏幕

默认左滑翻到下一页，右滑回到上一页。设置里改成**点击翻页**（见[设置](#设置)）后，点屏幕右半边翻到下一页、点左半边回到上一页，左右滑动也照样能用。换页时新页面从侧边滑入。从屏幕侧边开始的滑动仍保留给系统的退出手势。

屏幕只在手指抬起后才上报滑动，所以页面在手势结束后才开始滑动，不会跟着手指拖动。

| 页面 | 内容 |
| --- | --- |
| HOME | LAN 和 Tailscale IP，网关 / 公网 / DNS 状态，HDMI / USB 状态 |
| HOST | HDMI 分辨率和帧率，USB 连接状态和速度 |
| WEB | 二维码，扫码打开网页 |
| HEALTH | CPU（带走势曲线）、内存、磁盘、温度、运行时长 |
| NETWORK | 网关 / 公网 / DNS 延迟，以及已启用的网卡 |
| TOP | 占用 CPU 最多的进程 |
| SETTINGS | 翻页方式、自动息屏及息屏时长；点一行就切换到下一个选项 |

大号数字用矩形绘制的七段数码管字体，在屏幕上更清晰；`qr.py` 是给 WEB 页用的精简二维码编码器。

![屏幕的 7 个页面](../_docs/dashboard/panel.png)

只要 NanoKVM 报告了有效的输入分辨率，就认为 HDMI 已连接。视频采集只在有人打开 KVM 页面时才运行，其余时间 HOST 页显示 `IDLE`（网页上显示 "Capture: Idle"），但分辨率和帧率照常显示。

### 自动息屏

自动息屏默认开启：1 分钟没有触摸，屏幕就会变黑（也可以选 30 秒、5 分钟或 10 分钟）。息屏后第一次触摸只唤醒屏幕，不会翻页。息屏期间采样、连通性检测和网页都照常运行。

`appbase` 没有控制背光的接口，所以息屏是把屏幕画成全黑、唤醒前不再重绘；背光本身仍然亮着。

## Web 页面

App 打开期间，在同一局域网内用浏览器访问 `http://<LAN IP>:8080/apps/dashboard/`，或扫描屏幕 WEB 页的二维码；访问 `http://<LAN IP>:8080/` 也会自动跳转。

- 页面分 6 个标签：Overview、Target host、System、Network、Processes、Settings。
- 每 2 秒刷新一次，并绘制最近 10 分钟的 CPU、内存、温度和网速曲线。
- 主题可以选 System、Light 或 Dark，每个浏览器分别记住自己的选择。
- 只用原生 HTML/CSS/JS，不加载外部资源，没有外网也能用。
- 手机上，标签名放在值的上方；标签栏可以左右滑动，宽表格在卡片内部横向滚动，文字不会被挤成一词一行。

| Target host | System |
| --- | --- |
| ![Target host 标签](../_docs/dashboard/web-target.png) | ![System 标签](../_docs/dashboard/web-system.png) |
| **Network** | **Processes** |
| ![Network 标签](../_docs/dashboard/web-network.png) | ![Processes 标签](../_docs/dashboard/web-processes.png) |
| **Settings** | |
| ![Settings 标签](../_docs/dashboard/web-settings.png) | |

<details>
<summary>手机和深色主题</summary>

![360 px 宽手机上的 Network、System 和 Settings 标签](../_docs/dashboard/web-mobile.png)

![Overview 标签，深色主题](../_docs/dashboard/web-overview-dark.png)

</details>

Network 标签页可以从设备上发起 ping（3 个包）或 DNS 查询。目标必须是 IP 地址或主机名，同一时间只能运行一个，并且至少间隔 2 秒。

Web 服务只在 App 打开时运行：
- 退出 App 后服务停止、端口释放，页面会显示"离线"提示；
- 曲线历史只保存在内存里，每次打开 App 都从零开始。

为什么用单独的端口：NanoKVM 自带的 Web 服务（80/443 端口）不支持 App 在不修改系统文件的情况下添加页面，所以 App 使用自己的端口。页面里的接口都用相对路径，以后放到反向代理后面、挂在任意路径下也能直接用。

请用设备的 IP、主机名、`<主机名>.local` 或 Tailscale MagicDNS 名称（`<主机名>.<tailnet>.ts.net`）打开页面，地址栏里用其他域名会得到 `403`。这样别的网站就没法把自己的域名解析到设备上，借此读取页面或修改设置（DNS rebinding）。所以放在反向代理后面时，代理转发的 `Host` 也必须是上面这些名称之一（或 IP）。`POST` 接口只接受 `application/json`，别的网站的页面没法借你的浏览器向它们提交请求。

> **没有访问口令。** App 打开期间，同一局域网内的任何人都能看到这个页面，包括 IP、MAC 地址、Wi-Fi 网络名和进程名，也能修改[设置](#设置)。日志、命令行和其他 Tailscale 设备都不会显示。不用时请退出 App。

JSON 接口：
- `/apps/dashboard/api/status`：返回当前快照，以及 `?since=<seq>` 之后的历史；
- `/apps/dashboard/api/history`：返回全部历史；
- `/apps/dashboard/api/settings`：`GET` 读取设置；`POST` 一个 JSON 对象，修改其中列出的设置。

`POST` 请求必须带 `Content-Type: application/json`。

## 性能

只有在有人看的时候 App 才干活：面板亮着，或者 10 秒内有网页轮询过。面板息屏且没有网页打开时，不采样、不检测、不绘制，App 一直休眠，直到有触摸或网页请求。这段时间在曲线上留空，不补假数据；空闲之后的第一个网页请求会等一次新的采样（最多 1.5 秒）。

- **采样：** 每秒一次，只读取开销很小的 `/proc` 和 `/sys` 文件。
- **检测：** 慢操作（连通性检测、DNS、`ping`、`iw`、本机推流状态、`tailscale`）都在后台线程里，每 5 秒一次。超过 15 秒的结果显示为"未知"。
- **只给网页用的信息：** 服务、Tailscale、DNS 服务器每 15 秒刷新一次，进程列表每 2 秒一次。两者都只在有网页打开时采集；进程列表在面板亮着并停在 TOP 页时也会采集。
- **面板：** 每页只在显示内容变化时才重绘（HEALTH 每秒都变，其他页通常很少变），只有重绘后才写 framebuffer。两次采样之间 App 在触摸设备上等待，不再轮询。
- **网页：** 每个打开的网页每 2 秒轮询一次；同一次采样、进度相同的网页共用一份编码好的回复，设置也随这份回复一起返回。浏览器标签页隐藏时停止轮询。

## 连通性检测

后台线程每 5 秒检测一次：

| 项目 | 方法 |
| --- | --- |
| 网关 | TCP 连接 80 端口，被拒绝也算可达。连接超时时，如果设备上有 `ping`，就用 ping 再试一次；没有 ping 时显示"未知"，不会显示"失败"。 |
| 公网 | TCP 连接公网检测 IP（默认 `1.1.1.1`）的 443 端口。 |
| DNS | 解析 DNS 检测域名（默认 `www.cloudflare.com`），超过 3 秒算失败。 |

## 设置

在屏幕的 SETTINGS 页或网页的 Settings 标签里修改。设置保存在 App 目录下的 `settings.json`，重启后仍然保留，改完立即生效。

| 设置 | 默认值 | 在哪里改 | 说明 |
| --- | --- | --- | --- |
| 翻页方式 | 滑动 | 屏幕、网页 | 滑动：左右滑。点击：点左 / 右半屏，左右滑动也照样能用。 |
| 自动息屏 | 开 | 屏幕、网页 | 一段时间没有触摸就息屏。 |
| 息屏时长 | 1 分钟 | 屏幕、网页 | 30 秒、1 分钟、5 分钟或 10 分钟。 |
| 公网检测 IP | `1.1.1.1` | 网页 | 公网检测的目标 IP。只接受 IP，这样公网检测不依赖 DNS。 |
| DNS 检测域名 | `www.cloudflare.com` | 网页 | DNS 检测要解析的域名。 |

把公网检测 IP 或 DNS 检测域名留空，就会关闭对应的检测。`settings.json` 不存在或已损坏时使用默认值，问题会写入 App 日志。

Web 端口在 NanoKVM 网页的 **Settings > Apps** 中设置：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DASHBOARD_WEB_PORT` | `8080` | Web 页面端口。端口被占用时，屏幕上显示 `PORT ... IN USE`，其他功能照常运行。 |

## 开发

```bash
python -m unittest discover -s dashboard -p "test_*.py"
```

- `collector.py`、`settings.py` 和 `slide.py` 只用 Python 标准库，在任何系统上都能导入；采集的解析函数接收文本参数，不需要真机就能测试；页面滑动的测试用一个假的 framebuffer 覆盖全部 4 种旋转方向。
- 设置 `DASHBOARD_DEBUG=1` 后，每 10 秒向 stderr 打印一次最长的采样加重绘耗时。
- `_docs/dashboard/` 里的截图使用的是虚构地址。

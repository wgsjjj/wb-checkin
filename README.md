# WorkBuddy 多账号签到助手

一个跑在自己设备上的 WorkBuddy 每日签到面板：**多账号统一管理、一键批量签到、每天定时自动重试、积分与连续天数汇总**。

后端是单文件 Python（只依赖标准库 + `cryptography` 用于加密令牌），前端是一个静态页面，整包可以直接丢进软路由的 Docker 里长期运行。

```
┌─────────────────────────────────────────────┐
│  账号列表   今日状态 / 连续天数 / 积分余额   │
│  一键全部签到 · 单账号签到 · 一键测活        │
│  手机扫码添加账号（不用在电脑上复制粘贴）    │
│  定时 08:30 / 12:00 / 15:00 / 18:00 / 21:30 │
│  签到日志（成功 / 已签到 / 令牌失效 / 失败） │
└─────────────────────────────────────────────┘
```

---


## 0. 直接下载（不想装 Python）

到 [Releases](https://github.com/wgsjjj/wb-checkin/releases/latest) 下载：

| 文件 | 说明 |
| --- | --- |
| `wb-checkin-1.0.0-win64.zip` | **Windows 单文件 exe，解压双击就能用**，不用装 Python |
| `wb-checkin-1.0.0-source.zip` | 源码包，装了 Python 3.9+ 的机器（Windows / macOS / Linux）用 |

exe 版的数据存在它旁边的 `data/` 目录，整个文件夹拷走就能换机器继续用。
不想要黑色命令行窗口？双击包里的 `静默启动.bat`，它只开服务不弹浏览器。

> exe 是 PyInstaller 打的包，个别杀软会启发式误报，加信任即可；介意就用源码包，代码全在里面。

---


## 1. 先搞清楚一件事：多账号的令牌从哪来

WorkBuddy 桌面端会把登录态写在本地：

```
Windows: %LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop.info
macOS:   ~/Library/Application Support/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info
Linux:   ~/.local/share/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info
```

但**新版本把这个文件里的 `accessToken` / `refreshToken` 加密了**（`$wbEncrypted: 1` + AES-GCM 信封），也不再是明文 JSON。所以本工具不去碰客户端的加密文件，而是让你直接提供令牌——这也是多账号唯一稳妥的做法（一个客户端同一时间只持有一个账号的登录态）。

### 获取某个账号的令牌

1. 浏览器打开 <https://www.workbuddy.cn> 并登录**要签到的那个账号**
2. 按 `F12` 打开开发者工具 → 切到 **Network（网络）** 面板 → 筛选 `Fetch/XHR`
3. 在页面上随便点一下（比如打开成长中心），让它发出请求
4. 找到任意以 `/v2/` 开头的请求，点开 → **Headers → Request Headers**
5. 复制 `Authorization` 里 `Bearer ` 后面的那一长串，就是令牌

> 换一个账号就重复一次（无痕窗口 / 退出重登），把每个账号的令牌各存一份。

添加账号时也可以直接粘贴整段登录态 JSON，工具会自动提取 `accessToken`、`uid`、`domain`。如果粘贴的是加密信封，会明确提示"没解析到可用的 accessToken"。

---

## 2. 本地快速验证（Windows / macOS / Linux）

```bash
cd wb-checkin
python -m pip install cryptography      # 可选，装了才用 AES 加密令牌
python app.py
```

浏览器会自动打开 <http://127.0.0.1:8787>。没有 Python 就双击 `start.bat`（需先装 Python 3.9+）。

首次启动会创建 `data/`（SQLite 数据库 + 密钥文件）。想换端口：

```bash
WB_PORT=9000 python app.py
```

### 2.1 手机扫码添加账号

控制台点「添加账号」→ 右上角切到「手机扫码」，会出现一个二维码：

1. 手机微信/相机扫码，打开手机版添加页（`http://<局域网IP>:8787/m?a=<一次性凭证>`）
2. 在手机上粘贴令牌、填个备注名，点「添加账号」
3. 电脑端最多 2 秒就自动出现这个账号，弹窗会自己关掉

二维码 **10 分钟有效、只能提交一次**，过期重新切换一次标签页即可生成新的。

> **关于扫码登录的说明**：你可能会想"能不能直接微信扫码登录，自动把账号加进去"。技术上做不到——网页版登录走 `open.weixin.qq.com` 的微信 OAuth，回调地址被固定在 `workbuddy.cn`，授权完成后令牌只落在**你自己手机浏览器**的 localStorage 里，我的服务读不到；客户端侧那条设备授权链路（`/auth-v2/realms/copilot/signin/device-session`）依赖客户端生成的 `device_code`，模拟它属于伪造设备身份，会被风控识别，客户端一升级就失效。所以扫码的作用是"把添加页搬到手机上"，令牌仍需你从浏览器里复制一次。

**为什么用手机加更方便**：如果你有多个账号，通常是在手机上切换微信号/账号登录的，令牌也在手机上——直接粘进去比"发到电脑再粘"少一步。软路由部署后，手机、平板都能访问控制台所在服务，不必开着电脑。

> 如果控制台是用 `127.0.0.1` 打开的，二维码会自动让你选一个局域网 IP；用局域网 IP 打开时直接用当前地址。手机必须和服务器在同一个网络。

---

## 3. 部署到软路由（Docker）

镜像已经由 GitHub Actions 预构建好并推到 GHCR，**软路由上不需要编译、不需要装 Rust、x86 和 ARM 都能直接拉**：

```
ghcr.io/wgsjjj/wb-checkin:latest      # 多架构：linux/amd64 + linux/arm64
```

### 方式 A：DPanel 创建 Compose 应用（推荐）

DPanel 面板 →「Compose」→「创建」，把下面这段整段粘进去即可（也存成了仓库里的 `dpanel-compose.yaml`）：

```yaml
services:
  wb-checkin:
    image: ghcr.io/wgsjjj/wb-checkin:latest
    container_name: wb-checkin
    restart: unless-stopped
    ports:
      - "8787:8787"
    environment:
      TZ: Asia/Shanghai
      # 改成你自己的访问密码，留空 = 同网段任何人可打开控制台
      WB_PASSWORD: "change-me-123456"
      WB_DOMAIN: "www.workbuddy.cn"
    volumes:
      - wb-data:/data
    mem_limit: 256m
    logging:
      driver: json-file
      options:
        max-size: "5m"
        max-file: "2"

volumes:
  wb-data:
```

创建时把 `WB_PASSWORD` 改成自己的密码，端口 8787 被占用就改左边的 `"18787:8787"`。数据存在 `wb-data` 命名卷里，删容器不丢。

> 若拉取时报 `denied` / `unauthorized`：说明镜像包的可见性还是 Private。仓库已设为 Public，首次构建完成后镜像默认也是 Public；万一不是，去仓库右侧 **Packages** → 该包 → **Package settings** → Change visibility → Public。

### 方式 B：软路由上命令行 compose

```bash
mkdir -p /root/wb-checkin && cd /root/wb-checkin
curl -fsSLO https://raw.githubusercontent.com/wgsjjj/wb-checkin/main/dpanel-compose.yaml
mv dpanel-compose.yaml compose.yaml
# 改一下 WB_PASSWORD
vi compose.yaml
docker compose up -d
```

访问 `http://软路由IP:8787`。

### 方式 C：完全离线/自建镜像

想自己构建（或改代码后自建）：

```bash
git clone https://github.com/wgsjjj/wb-checkin.git
cd wb-checkin
docker compose up -d --build        # docker-compose.yml 里是 build: .
```

ARM 设备上不建议直接 `--build`——`cryptography` 可能现场编译 Rust，软路由扛不住。用预构建镜像即可。

### 更新到新版本

```bash
docker compose pull && docker compose up -d     # 或在 DPanel 里点「重新拉取镜像 + 重建」
```

### 部署后要检查的三件事

| 检查项 | 怎么确认 |
| --- | --- |
| 时区对不对 | 页面底部「服务时间」应等于北京时间，否则定时会偏 8 小时 |
| 密码生效没 | 打开页面应先要求输入访问密码（环境变量 `WB_PASSWORD` 为空则不校验） |
| 数据持不持久 | 卷 `wb-data` 里应有 `wb.db` 和 `.secret`，重建容器不丢 |

---

## 4. 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `WB_PORT` | `8787` | 监听端口 |
| `WB_HOST` | `127.0.0.1` | 监听地址，Docker 里固定 `0.0.0.0` |
| `WB_PASSWORD` | 空 | 访问密码，设置后进控制台需登录；局域网/公网部署**务必设置** |
| `WB_SECRET` | 空 | 令牌加密主密钥。留空则自动生成 `data/.secret`；数据目录迁移时带上它 |
| `WB_DATA_DIR` | `./data` | 数据库与密钥目录，容器内为 `/data` |
| `WB_DOMAIN` | `www.workbuddy.cn` | 默认接口域名，添加账号时可单独指定 |
| `WB_NO_BROWSER` | 未设置 | `1` 表示启动不自动开浏览器（容器里默认开启该行为） |

---

## 5. 定时策略

默认每天 **08:30 / 12:00 / 15:00 / 18:00 / 21:30** 各跑一轮，可在面板里改成任意时间点（逗号分隔）。

- 每轮只跑「启用」状态的账号，多账号并发执行（最多 5 个）
- 已签到的账号会直接跳过，**重复调用不会重复发积分**（接口本身幂等）
- 某个时间点失败不要紧，后面的时间点会自动补
- 同一时间点当天只触发一次，重启容器也不会重复跑
- 自动签到开关、时间点都持久化在数据库里

关掉自动、只手动点也完全没问题。

---

## 6. 数据与安全

- 令牌用 **AES-GCM** 加密后存进 SQLite（`data/wb.db`），密钥在 `data/.secret`（权限 600）
- 本地 Windows 若没装 `cryptography`，退回用系统 **DPAPI** 加密（绑定当前用户，换机器解不开）
- 所有请求都在本机/自己的服务器上发出，不经过任何第三方；数据不上传
- 服务默认只监听 `127.0.0.1`；容器里监听 `0.0.0.0`，**靠 `WB_PASSWORD` 挡人**
- `.env`、`data/` 已被 `.dockerignore` 和 `.gitignore` 排除，别把它们提交到仓库

### token 会过期

接口返回的令牌有效期约 **28 天**。过期后账号卡片会显示「令牌失效」，重新按第 1 节的步骤复制一次新令牌，点「编辑」粘贴进去即可，积分和连续天数记录不会丢。

---

## 7. 接口说明（便于自行扩展）

签到相关接口（域名取自账号的 `domain` 字段，切勿硬编码成 `copilot.tencent.com`，那个会 404）：

```
POST /v2/billing/meter/checkin-activity-status   查询今日状态
POST /v2/billing/meter/daily-checkin             领取每日签到积分
```

请求头：

```
Authorization: Bearer <token>
X-User-Id: <uid>          # 可选
Content-Type: application/json
```

返回示例：

```json
{ "code": 0, "data": { "today_checked_in": false, "streak_days": 3, "daily_credit": 100 } }
```

已签到会返回 HTTP 400 + `code: 10001`，`msg: "今天已签到，请明天再来"`。

本工具自身的 API 都在 `/api/` 下，`GET /api/overview` 一次性返回账号、统计、设置与日志，可直接用于二次开发。

---

## 8. 常见问题

**Q：为什么不能自动读取本机客户端登录态？**
A：当前版本的客户端把令牌加密了，解密密钥不落在可读文件里。硬去逆它风险高，也不稳定（客户端一升级就废）。手动提供令牌虽然多一步，但每次能用几个月，而且支持任意多个账号、任意设备。

**Q：容器起来了但页面打不开？**
A：先看 `docker logs wb-checkin`；再确认软路由防火墙/端口映射；用 `curl -I http://127.0.0.1:8787` 在软路由本机上试。

**Q：手机扫码打开是空白 / 打不开？**
A：三种可能是：① 控制台是用 `127.0.0.1` 打开的，二维码里的地址手机访问不到——改用软路由/电脑的局域网 IP 打开控制台，或在扫码弹窗里手动选一个 IP；② 手机不在同一个局域网；③ 软路由防火墙拦了 8787 端口。

**Q：签到成功但积分显示没变？**
A：签到后会再查一次状态刷新余额，如果上游余额字段变了，日志里能看到原始返回。也可能存在到账延迟。

**Q：会被封号吗？**
A：只调用客户端自己会调用的官方接口，每个账号每天最多成功签到一次，频率远低于人工点击。但仍然属于自动化操作，请自行评估风险，不要用多账号做异常刷分。

**Q：连续第 7 天有额外奖励吗？**
A：这是上游活动规则决定的，本工具只负责执行签到，不干预奖励判定。

---

## 目录结构

```
wb-checkin/
├── app.py                    后端（HTTP 服务 + 签到逻辑 + 定时调度 + 扫码会话）
├── static/index.html         控制台页面
├── static/m.html             手机端添加页
├── static/qrcode.js          二维码生成（qrcode-generator, MIT，已本地化）
├── Dockerfile
├── docker-compose.yml        源码自建用
├── dpanel-compose.yaml       DPanel 用（拉 GHCR 镜像）
├── requirements.txt
├── .env.example
├── start.bat                 Windows 源码启动
├── wb.ico / wb-version.txt   exe 图标与版本信息
├── packaging/
│   ├── make_icon.py          生成 wb.ico
│   ├── pack_local.py         打 Windows 本地部署包
│   └── 使用说明.txt           打包进 zip 的说明
├── .github/workflows/        多架构镜像构建 + 冒烟测试
└── data/                     运行时生成：wb.db / .secret
```

---

## 自己打包 exe

```bash
python -m pip install pyinstaller cryptography
python -m PyInstaller --noconfirm --clean --onefile --console \
  --name wb-checkin --icon wb.ico --version-file wb-version.txt \
  --add-data "static;static" app.py
python packaging/pack_local.py      # 生成 dist/*.zip
```

打包时**必须**装上 `cryptography`，否则 exe 里的令牌会退回 Windows DPAPI 加密——
那样 `data/` 目录就不便携了，换台电脑解不开。


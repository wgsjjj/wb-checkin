# WorkBuddy 多账号签到助手

一个跑在自己设备上的 WorkBuddy 每日签到面板：**多账号统一管理、一键批量签到、每天定时自动重试、积分与连续天数汇总**。

后端是单文件 Python（只依赖标准库 + `cryptography` 用于加密令牌），前端是一个静态页面，整包可以直接丢进软路由的 Docker 里长期运行。

```
┌─────────────────────────────────────────────┐
│  账号列表   今日状态 / 连续天数 / 积分余额   │
│  一键全部签到 · 单账号签到 · 一键测活        │
│  官方登录添加：微信扫码 / 手机号验证码      │
│  定时 08:30 / 12:00 / 15:00 / 18:00 / 21:30 │
│  签到日志（成功 / 已签到 / 令牌失效 / 失败） │
└─────────────────────────────────────────────┘
```

---


## 更新记录

**1.3.1** · 2026-11-21
- **修复扫码登录后账号名变成「账号 11212126」这种数字**：1.2.0 改成从用户菜单读昵称后，只在登录成功那一瞬间读一次；页面还没渲染完，读到的永远是空值，于是回落到「账号 + 月日时分」。现在改成轮询等待（最多约 12 秒），并且补了头像 `alt` 作为兜底选择器
- 兜底名格式调整为 `账号 11-21 21:26`，八位纯数字确实难认

**1.3.0** · 2026-11-13
- **Docker 镜像内置 chromium，容器里也能扫码登录**：之前容器没有浏览器，「官方登录」直接报「本机没找到 Chrome 或 Edge」；现在镜像默认装好 chromium，二维码照常显示在控制台弹窗里
- 容器内自动补 `--no-sandbox` / `--disable-dev-shm-usage`（容器默认 root、`/dev/shm` 只有 64MB，不加这两个参数 chromium 起不来或一加载就崩）
- 新增 `WB_CHROME_PATH` 指定浏览器、`WB_CHROME_ARGS` 追加启动参数
- 浏览器启动失败时把 chromium 自己的报错带回前端，不再只说「启动失败」
- 容器里点「手机号 / 企业微信」登录会直接提示不支持（没有图形界面），不再静默卡住
- 内存限制从 256m 提到 1g，并加 `shm_size: 256m`；存储紧张可用 `WB_WITH_BROWSER=0` 构建不带浏览器的镜像
- 镜像体积增加约 400~500MB

**1.2.0** · 2026-11-13
- **官方登录改成无头模式**：后台跑，二维码直接显示在控制台弹窗里，不再弹出浏览器窗口
- **支持网页版 Cookie 凭据**：网页登录根本不用 Bearer 令牌，之前抓不到凭据就是因为找错了地方；Cookie 还要带上浏览器原样 UA
- 账号名改从用户菜单（`user-menu-trigger-name`）读取，不再靠猜页面文字（之前抓到过「引用来源」这种）
- **积分拆成两个数**：新增 `/billing/meter/get-user-resource-summary` 拿真实余额，「签到累计」单独一个字段
- 修复：登录前的空会话被误判为已登录；二维码出来前的状态文案
- 手机号 / 企业微信登录保留窗口模式（需要人工输入）

**1.1.0** · 2026-11-13
- 新增官方登录（CDP 驱动浏览器扫码），打包 Windows exe 与 Release

**1.0.0** · 2026-11-13
- 首个版本：多账号管理、一键批量签到、定时重试、手机扫码添加、Docker / GHCR 部署

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


## 1. 添加账号：三种方式

### 方式一：官方登录（推荐，不用碰令牌）

点「添加账号」→ 默认就是**官方登录**标签 → 点「微信扫码登录」。

程序会在**后台静默**起一个无头浏览器（不弹窗、不显示任何窗口），把官方登录页上的微信二维码截出来，直接显示在弹窗里。手机扫那个码就行：

- 扫完 → 官方页面完成授权 → 程序立刻把登录态读走 → 账号出现在列表里 → 后台浏览器自动关闭
- 想用**手机号 / 企业微信 / QQ / 邮箱**登录：点下面那个灰色按钮，这种才需要弹出窗口（因为得有人去输号码和验证码）

**它是怎么做的**：workbuddy.cn 的网页登录是在浏览器里跳微信授权、回调后由页面自己写入登录态，任何服务端都拿不到。所以这里用 Chrome 调试协议（CDP）起一个真实浏览器跑完整个官方流程，登录后在同一个页面上下文里把凭据读出来——不逆向客户端加密、不伪造设备身份，只是替你把「登录完再手动复制 token」这一步省了。

读到的凭据是网页版的 **httpOnly Cookie + 浏览器 UA**（网页版不用 Bearer 令牌，两个都要，网关会校验 UA），加密后存本地。

> 需要后端所在机器有可用浏览器：桌面端用系统里的 Chrome / Edge（Windows 自带 Edge）；
> **Docker 部署用镜像内置的 chromium**（1.3.0 起默认装好，源码自建也一样），
> 容器里扫码登录已经可用。想用指定的浏览器就设 `WB_CHROME_PATH`。
> 如果软路由内存 / 存储实在紧张，可以 `--build-arg WB_WITH_BROWSER=0` 构建不带浏览器的镜像，
> 此时只能用方式二或方式三添加账号。

### 方式二：从浏览器复制令牌

如果你已经在自己的浏览器里登录了，也可以手动粘一次：

1. 按 `F12` 打开开发者工具 → **Network（网络）** → 筛选 `Fetch/XHR`
2. 页面上随便点一下（比如打开成长中心），让它发出请求
3. 找到任意 `/v2/` 开头的请求 → **Headers → Request Headers**
4. 复制 `Authorization` 里 `Bearer ` 后面那一长串

粘贴时可以只粘那串 token，也可以粘整段登录态 JSON（会自动提取 `accessToken` / `uid` / `domain`）。

> 注意：网页版登录态是 Cookie 不是 Bearer，所以方式二适用于**桌面客户端**的令牌；从网页 F12 里复制 `Authorization` 可能压根没有这个头。


### 方式三：手机扫码填写

控制台点「添加账号」→「手机扫码」，手机扫码后在手机上粘贴令牌。适合"账号本来就在手机微信里登录"的场景。

### 令牌从哪来（技术说明）

WorkBuddy 桌面端会把登录态写在本地：

```
Windows: %LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop.info
macOS:   ~/Library/Application Support/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info
Linux:   ~/.local/share/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info
```

但**新版本把里面的 `accessToken` / `refreshToken` 加密了**（`$wbEncrypted: 1` + AES-GCM 信封）。本工具不去碰这个文件，也解不开它——所以才有上面三种正规路径。


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
    # 内置 chromium 之后扫码要几百 MB 内存，256m 会被 OOM 干掉；
    # 不用浏览器（只粘贴凭据）时可以调回 256m
    mem_limit: 1g
    # chromium 依赖 /dev/shm，容器默认只有 64MB
    shm_size: 256m
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

### 方式 C：拉不动镜像时，直接在宿主机构建（国内网络推荐）

先判断是不是「拉不动」：

| 报错 | 含义 |
|---|---|
| `unexpected EOF`、进度到 100% 后又 `Retrying` 从 0 开始 | 大层（chromium 那层约 289MB）传输末尾被掐断，校验失败整层重下 |
| `TLS handshake timeout`、`i/o timeout`、`connection reset` | 连不上 ghcr.io |
| `no space left on device` | 磁盘不够，先把空间腾出来再看 |

换国内镜像站（`ghcr.nju.edu.cn`、`ghcr.dockerproxy.net`、`ghcr.1ms.run`）能缓解，但不一定解决——**很多线路对长时间大流量传输不稳定，换个源仍然卡在同一层**。这种情况下最省事的办法是让宿主机自己装 chromium，因为软件源都在国内，速度能到几 MB/s。

#### C-1. 把源码放到宿主机

宿主机能连 GitHub 就直接 clone：

```bash
git clone https://github.com/wgsjjj/wb-checkin.git
cd wb-checkin
```

连不上就在自己的电脑上打包再传（`192.168.100.1` 换成你的设备地址）：

```bash
# 在电脑上，仓库目录里执行
tar -czf wb-src.tgz --exclude=.git --exclude=data .
scp wb-src.tgz root@192.168.100.1:/tmp/

# 在宿主机上
mkdir -p /root/wb-checkin-src && tar -xzf /tmp/wb-src.tgz -C /root/wb-checkin-src
cd /root/wb-checkin-src
```

只传源码就够，**不需要传镜像**，几百 KB 而已。

#### C-1b. 另一种做法：本机构建后传镜像

如果你的电脑装了 Docker（Windows / macOS 的 Docker Desktop 也算），也可以在本机构建再传过去，宿主机就不用装编译依赖了：

```bash
# 本机，仓库目录里执行
docker build -f Dockerfile.cn -t wb-checkin:latest .
docker save wb-checkin:latest | gzip > wb-checkin.tar.gz

scp wb-checkin.tar.gz root@192.168.100.1:/tmp/

# 宿主机
docker load -i /tmp/wb-checkin.tar.gz
docker images | grep wb-checkin
```

两个前提：

- **架构必须一致**。本机和宿主机都是 x86_64 才能直接传。ARM 设备（N1、部分群晖、树莓派）要在本机加 `--platform linux/arm64` 构建，会走 QEMU 模拟，慢好几倍；这种情况不如直接在宿主机上构建。
- **压缩后仍有三四百 MB**。网络只比 ghcr 好一点的话，传源码（几百 KB）让宿主机自己装更快，也更不容易在传输末尾断掉。

#### C-2. 构建

仓库里带了 `Dockerfile.cn`，apt 与 pip 全部走阿里云 https 源，直接可用：

```bash
docker build -f Dockerfile.cn -t wb-checkin:latest .
```

软路由上大约 10~20 分钟（x86 实测），时间主要花在下载和安装 chromium 的九十多个依赖包。构建末尾会自动跑 `chromium --version`，**能打印版本号才算成功**。

> 想换源：`--build-arg APT_MIRROR=mirrors.tuna.tsinghua.edu.cn`（清华）或 `mirrors.ustc.edu.cn`（中科大）。
> 存储紧张、不需要扫码登录：加 `--build-arg WB_WITH_BROWSER=0`，镜像能小 400MB 以上，但「官方登录」就用不了，只能粘贴凭据。
> 用官方 `Dockerfile` 构建时要自己把 apt 源换成国内 https 源，否则 apt 会慢到像卡死——**必须是 https**，不少线路把 80 端口限速到二三十 KB/s。
> 依赖只有一个 `cryptography`，aarch64 有预编译 wheel，软路由上直接装即可；如果日志里出现 `Building wheel for cryptography` 并开始调 cargo/rustc，说明在本地编译，会慢很多（可用 `APT_MIRROR` 换更快的源，或改用预构建镜像）。

#### C-3. 用本地镜像启动

关键：compose 里的 `image` 必须写成**本地标签**，不能留 `ghcr.io/...`，否则 `up -d` 会去仓库拉取，直接失败。

```yaml
services:
  wb-checkin:
    image: wb-checkin:latest      # 构建出来的本地镜像，不是 ghcr
    container_name: wb-checkin
    restart: unless-stopped
    ports:
      - "8787:8787"
    environment:
      TZ: Asia/Shanghai
      WB_PASSWORD: "换成你自己的密码"
      WB_DOMAIN: "www.workbuddy.cn"
    volumes:
      - wb-data:/data
    mem_limit: 1g                 # 扫码时内存峰值几百 MB，256m 会被 OOM
    shm_size: 256m                # chromium 依赖 /dev/shm，默认只有 64MB

volumes:
  wb-data:
```

```bash
docker compose up -d
docker exec wb-checkin chromium --version     # 有版本号说明浏览器装好了
```

#### C-4. 以后再升级

本地构建的镜像不会自己变新，改了代码或拉不到新版本时：

```bash
cd /root/wb-checkin-src
# 更新源码：git pull，或者重新 scp 覆盖 app.py / browser_login.py / static/
docker build -f Dockerfile.cn -t wb-checkin:latest .
docker compose up -d --force-recreate
```

`--force-recreate` 不能省：标签名没变（都是 `latest`），不加这个参数 compose 可能认为容器无需重建，还在跑旧镜像。

验证装进去的确实是新版本：

```bash
curl -s http://127.0.0.1:8787/api/me      # version 字段
```

### 更新到新版本

**用官方镜像的**：

```bash
docker compose pull && docker compose up -d     # 或在 DPanel 里点「重新拉取镜像 + 重建」
```

两个容易踩的点：

- `docker compose up -d` **不会自己去仓库检查更新**。本地已有 `:latest` 标签时它直接拿本地那份启动，配置改了、容器也重建了，代码还是旧的。`docker compose pull` 要单独执行。
- 想省事可以在 compose 里加 `pull_policy: always`，但网络不通时 `up -d` 会直接失败而不是退回本地镜像，软路由上不建议默认打开。

**本地构建的**：见上面 C-4。

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
| `WB_NO_BROWSER` | 未设置 | `1` 表示启动不自动开浏览器（容器里默认开启该行为）；与「官方登录」无关 |
| `WB_CHROME_PATH` | 自动探测 | 指定浏览器，完整路径或命令名都行。容器里默认用镜像内置的 `/usr/bin/chromium` |
| `WB_CHROME_ARGS` | 空 | 额外启动参数，空格分隔。例如挂代理 `--proxy-server=http://192.168.1.2:7890`；容器内的 `--no-sandbox` 会自动补 |

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

## 6.5 兼容性 / 能跑在哪

| 部分 | 要求 |
| --- | --- |
| 控制台页面 | 任何现代浏览器：Chrome / Edge / Firefox / Safari / 手机浏览器都行，无框架无构建 |
| 后端 | Python 3.9+，Windows / macOS / Linux；Windows 另有免安装 exe |
| 官方扫码登录 | **需要后端所在机器有可用浏览器**：桌面端用系统的 Chrome / Edge，容器里用镜像内置的 chromium（1.3.0 起默认装好）。手机号 / 企业微信登录需要有图形界面，容器里只能用扫码 |

控制台可以换任何电脑、任何浏览器打开（`http://后端IP:8787`），但「扫码自动抓凭据」这件事
必须在跑后端的那台机器上完成——是它去启动浏览器的。

> **换机器要注意**：网页 Cookie 凭据除了绑定 UA，是否还绑定 IP 尚未验证。把 `data/` 整体搬到
> 另一台机器/另一条网络后，先点一次「测活」：能读到余额说明没问题；返回 401 就说明有 IP 绑定，
> 只能在常用网络下用。

---

## 7. 接口说明（便于自行扩展）

签到相关接口（域名取自账号的 `domain` 字段，切勿硬编码成 `copilot.tencent.com`，那个会 404）：

```
POST /v2/billing/meter/checkin-activity-status   查询今日状态
POST /v2/billing/meter/daily-checkin             领取每日签到积分
```

另一种凭据是**网页版 Cookie**，此时请求头换成：

```
Cookie: session=...; session_2=...
User-Agent: <浏览器原样 UA>   # 网关会校验 UA，换成别的直接 401
Content-Type: application/json
```

余额和签到积分是两个接口、两个概念，别搞混：

```
POST /billing/meter/get-user-resource-summary    真实可用余额（注意：没有 /v2 前缀）
POST /v2/billing/meter/checkin-activity-status   今日状态 + 活动累计
```

`get-user-resource-summary` 里的 `data.TotalCredits` 才是账户余额（如 `"69.23000501"`），
而签到接口里的 `data.total_credits` 是**本次活动累计获得**（连续 28 天 × 100 = 2800），
不扣掉你消耗掉的积分。页面上分别显示为「余额」和「签到累计」。

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

**Q：积分数字和我在产品里看到的不一样？**
A：看的是哪个数。「余额」来自 `/billing/meter/get-user-resource-summary`，是当前可用积分；「签到累计」来自签到活动接口，只是本次活动的累计发放量（连续天数 × 每日积分），不扣消耗。两个数对不上是正常的。

**Q：点「微信扫码登录」没反应 / 提示找不到浏览器？**
A：官方登录需要后端所在机器有可用浏览器。桌面端：Windows 自带 Edge，Linux 桌面装 `chromium` 或 `google-chrome`；容器里从 1.3.0 起镜像内置了 chromium，**扫码登录可以直接用**。如果容器还是提示找不到浏览器，说明用的是旧镜像或构建时关了浏览器，`docker exec wb-checkin chromium --version` 确认一下；报错信息现在会带上浏览器自己的输出，能直接看出缺什么库。

**Q：扫码成功了但控制台一直显示等待？**
A：先看状态那行字。正常流程是「正在加载二维码…」→「请用微信扫描二维码」→ 扫码后 2 秒内变「登录成功」。如果卡在「登录中，等待页面跳转…」，说明页面离开登录页了但没检测到登录标记，把这句话告诉我。

**Q：容器起来了但页面打不开？**
A：先看 `docker logs wb-checkin`；再确认软路由防火墙/端口映射；用 `curl -I http://127.0.0.1:8787` 在软路由本机上试。

**Q：更新了镜像，页面上的版本号还是旧的？**
A：两种原因。① 用官方镜像：`docker compose up -d` **不会去仓库检查更新**，本地已有 `:latest` 就直接拿本地的启动，必须单独执行 `docker compose pull`；② 本地构建：标签名没变（都叫 `latest`），要 `docker compose up -d --force-recreate`，不加这个参数容器不重建。确认办法：`curl -s http://127.0.0.1:8787/api/me` 看 `version` 字段。

**Q：扫码加进来的账号名是「账号 11212126」这种八位数字？**
A：1.3.0 及更早版本的问题。那是「账号 + 月日时分」的兜底名——扫码成功后页面刚跳转，左下角用户菜单还没渲染出来，昵称读了个空就回落了。1.3.1 起改成轮询等待昵称出现（最多约 12 秒），新加的账号会直接用页面上的昵称。已经加好的账号改名即可：卡片右下角「编辑」，或 `PUT /api/accounts/<id>` 带 `{"name":"新名字"}`。

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
├── browser_login.py          官方登录：CDP 驱动无头浏览器，扫码后读走凭据
├── static/index.html         控制台页面
├── static/m.html             手机端添加页
├── static/qrcode.js          二维码生成（qrcode-generator, MIT，已本地化）
├── Dockerfile
├── Dockerfile.cn              国内网络构建版（apt/pip 走阿里云 https 源）
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
  --add-data "static;static" --hidden-import browser_login app.py
python packaging/pack_local.py      # 生成 dist/*.zip
```

产物：`dist/wb-checkin.exe` 单文件，约 14 MB；`pack_local.py` 会把它连
`使用说明.txt`、`静默启动.bat` 一起压成 `wb-checkin-<版本>-win64.zip`，
另出一份含 `browser_login.py` 的源码包。

`--hidden-import browser_login` 不能省：这个模块是在函数里延迟导入的，
显式声明可以保证扫码登录一定进包。

打包时**必须**装上 `cryptography`，否则 exe 里的令牌会退回 Windows DPAPI 加密——
那样 `data/` 目录就不便携了，换台电脑解不开。


# 阿里云 ECS 公网 IP 临时部署手册

本文档用于把个人版 B 站 AI 助手临时部署到阿里云 ECS，并通过公网 IP 访问。当前阶段没有域名，不做正式 HTTPS；服务只给自己用，所以安全边界放在阿里云安全组、Nginx Basic Auth 和本机回环监听上。

## 部署边界

- ECS 系统：Ubuntu 22.04 或 Ubuntu 24.04。
- 访问方式：`http://<ECS_PUBLIC_IP>`，先走公网 IP 临时访问。
- 入口层：Nginx 监听 `80`，开启 Basic Auth。
- 应用层：FastAPI 只监听 `127.0.0.1:8000`，不开放 `8000/tcp` 到公网。
- 数据迁移：首次上云不迁移本机 `data/*.db`、`data/output/`、音频和历史任务。
- 后续域名：域名解析到中国内地 ECS 并对外提供 Web 服务前，先完成 ICP 备案。

阿里云备案说明可参考官方文档：

- [ICP备案流程](https://help.aliyun.com/zh/icp-filing/basic-icp-service/user-guide/icp-filing-application-overview)
- [个人网站备案快速入门](https://help.aliyun.com/zh/icp-filing/basic-icp-service/getting-started/quick-start-for-icp-filing-for-personal-websites)

阿里云安全组说明可参考官方文档：

- [安全组规则](https://help.aliyun.com/zh/ecs/user-guide/security-group-rules/)
- [使用安全组](https://help.aliyun.com/zh/ecs/user-guide/start-using-security-groups)

## 1. 安全组

在阿里云 ECS 控制台的安全组入方向只保留必要端口：

| 协议 | 端口 | 授权对象 | 用途 |
| --- | --- | --- | --- |
| TCP | `22/22` | 你的公网 IP `/32` | SSH 登录 |
| TCP | `80/80` | 你的公网 IP `/32` | Nginx 临时公网入口 |

不要开放：

- `8000/tcp`：这是 uvicorn 内部端口，只允许 Nginx 在服务器本机访问。
- `0.0.0.0/0` 到 `22`：SSH 不要对全网开放。
- `0.0.0.0/0` 到 `80`：临时 IP 访问阶段不需要让所有人都能访问。

如果你的家庭或办公公网 IP 会变，变更网络后需要同步更新安全组授权对象。

## 2. 安装系统依赖

登录服务器后执行：

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git nginx apache2-utils ffmpeg
python3 --version
ffmpeg -version
```

安装 `yt-dlp` 推荐放在系统路径中：

```bash
sudo python3 -m pip install --upgrade yt-dlp
yt-dlp --version
```

如果服务器启用了 PEP 668 限制，系统级 `pip install` 可能被拒绝。此时用 pipx 安装：

```bash
sudo apt install -y pipx
sudo pipx ensurepath
sudo pipx install yt-dlp
sudo ln -sf /root/.local/bin/yt-dlp /usr/local/bin/yt-dlp
yt-dlp --version
```

## 3. 拉取代码

下面以 `/opt/mysakura` 作为部署目录。仓库地址按你实际 Git 远程地址替换。

```bash
sudo mkdir -p /opt/mysakura
sudo chown -R "$USER":"$USER" /opt/mysakura
git clone <YOUR_GIT_REPOSITORY_URL> /opt/mysakura
cd /opt/mysakura
git checkout v3
```

创建虚拟环境并安装 Python 依赖：

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```

## 4. 创建 Linux 版 `.env`

复制模板：

```bash
cp deploy/env.alicloud.example .env
chmod 600 .env
```

编辑 `.env`：

```bash
nano .env
```

必须确认这些 Linux 路径和部署约束：

```dotenv
APP_HOST=127.0.0.1
APP_PORT=8000
YT_DLP_BIN=yt-dlp
YT_DLP_COOKIES_FILE=data/bilibili-cookies.txt
FFMPEG_BIN=ffmpeg
ASR_PROVIDER=aliyun_paraformer
BILI_ENABLE_LISTENER=false
```

继续填写：

- `ALIYUN_DASHSCOPE_API_KEY`：中国内地（北京）地域的百炼 API Key。
- `ALIYUN_OSS_ACCESS_KEY_ID` / `ALIYUN_OSS_ACCESS_KEY_SECRET` / `ALIYUN_OSS_ENDPOINT` / `ALIYUN_OSS_BUCKET`：用于临时上传无字幕视频音频。
- `DEEPSEEK_API_KEY`：摘要模型 Key。
- `SMTP_*` / `MAIL_FROM` / `MAIL_TO`：发件邮箱配置。

建议先保持：

```dotenv
KEEP_AUDIO_AFTER_SUCCESS=false
BILI_ENABLE_LISTENER=false
```

主链路跑通后，再考虑开启 B 站 `@我` 监听。

如果 ECS 上 `yt-dlp` 解析 B 站视频返回 `HTTP Error 412: Precondition Failed`，说明服务器 IP 触发了 B 站风控。用浏览器扩展导出 `bilibili.com` 的 Netscape `cookies.txt`，上传到：

```bash
mkdir -p /opt/mysakura/data
vim /opt/mysakura/data/bilibili-cookies.txt
chmod 600 /opt/mysakura/data/bilibili-cookies.txt
```

然后先用命令验证：

```bash
yt-dlp --cookies /opt/mysakura/data/bilibili-cookies.txt \
  --dump-single-json --no-playlist \
  "https://www.bilibili.com/video/BV17UG46BEj2"
```

能输出 JSON 后，保持 `.env` 中：

```dotenv
YT_DLP_COOKIES_FILE=data/bilibili-cookies.txt
```

这个文件只给 `yt-dlp` 解析和下载用，和 `.env` 里的 `BILI_COOKIE` 不是同一个配置。不要把 `bilibili-cookies.txt` 提交到 Git。

## 5. 配置 systemd

复制服务模板：

```bash
sudo cp deploy/mysakura.service /etc/systemd/system/mysakura.service
sudo systemctl daemon-reload
sudo systemctl enable mysakura
sudo systemctl start mysakura
```

检查状态：

```bash
systemctl status mysakura --no-pager
journalctl -u mysakura -n 100 --no-pager
curl http://127.0.0.1:8000/api/tasks
```

期望 `curl` 返回类似：

```json
{"tasks":[]}
```

如果这里不通，先不要配 Nginx，优先看 `journalctl` 中的 Python 异常、缺失依赖或 `.env` 配置。

## 6. 配置 Nginx Basic Auth

创建访问账号。下面的 `mysakura` 是登录用户名，可以改：

```bash
sudo htpasswd -c /etc/nginx/.htpasswd-mysakura mysakura
```

复制 Nginx 模板：

```bash
sudo cp deploy/nginx-mysakura-ip.conf /etc/nginx/sites-available/mysakura
sudo ln -sf /etc/nginx/sites-available/mysakura /etc/nginx/sites-enabled/mysakura
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl reload nginx
```

服务器本机验证：

```bash
curl -I http://127.0.0.1/
```

期望看到 `401 Unauthorized`，说明 Basic Auth 已经生效。

然后在本机浏览器打开：

```text
http://<ECS_PUBLIC_IP>
```

输入 Basic Auth 用户名和密码后，应该看到 B 站 AI 助手页面。

## 7. 主链路验证

按成本从低到高验证：

1. 提交一个有官方字幕的视频，确认任务成功、Markdown 落盘、SMTP 邮件发送成功。
2. 提交一个无字幕视频，确认链路经过 `yt-dlp -> ffmpeg -> OSS -> Paraformer -> DeepSeek -> SMTP`。
3. 查看运行日志：

```bash
journalctl -u mysakura -f
```

4. 查看运行产物：

```bash
ls -lah data/
ls -lah data/output/
```

## 8. 常见问题

### 页面打不开

- 先看阿里云安全组是否允许你的公网 IP 访问 `80/tcp`。
- 再看 Nginx 是否运行：`systemctl status nginx --no-pager`。
- 再看应用是否运行：`systemctl status mysakura --no-pager`。
- 不要通过开放 `8000/tcp` 解决问题，先定位 Nginx 到 uvicorn 的反向代理链路。

### `yt-dlp` 或 `ffmpeg` 找不到

确认 `.env` 是 Linux 命令名，而不是 Windows 绝对路径：

```dotenv
YT_DLP_BIN=yt-dlp
YT_DLP_COOKIES_FILE=data/bilibili-cookies.txt
FFMPEG_BIN=ffmpeg
```

然后确认命令可执行：

```bash
which yt-dlp
which ffmpeg
```

### SMTP 发信失败

阿里云 ECS 的 `25/tcp` 通常受限制，当前项目默认使用 `465` SSL 端口。优先使用：

```dotenv
SMTP_PORT=465
SMTP_USE_SSL=true
```

### 磁盘空间增长

默认 `KEEP_AUDIO_AFTER_SUCCESS=false`，成功任务会删除本地音频；失败任务会保留音频，方便排查和重试。长期运行后可以定期检查：

```bash
du -h --max-depth=2 data | sort -h
```

## 9. 后续切换域名和 HTTPS

有域名后不要直接把未备案域名解析到中国内地 ECS 对外提供服务。推荐顺序：

1. 完成域名实名认证和 ICP 备案。
2. 把域名解析到 ECS 公网 IP。
3. 修改 Nginx `server_name` 为备案域名。
4. 申请并配置 HTTPS 证书，可以使用 Certbot 或阿里云证书服务。
5. 安全组继续按需保留 IP 白名单；如果未来要给别人用，再重新设计用户系统、权限和审计。

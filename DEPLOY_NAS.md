# WebDAV Downloader — NAS 部署指南

## 前提条件

- NAS 为 **x86（Intel/AMD）架构**（如极空间 Z4S、Z2S 等）
- 电脑已安装 **Docker Desktop**（[点此下载](https://www.docker.com/products/docker-desktop/)），且已启动（任务栏有鲸鱼图标）

---

## 第一步：在电脑上构建镜像

打开 **PowerShell** 或 **CMD**，执行：

```bat
cd "这里替换成项目的实际路径\webdav-downloader"
docker build -t webdav-downloader .
docker save webdav-downloader -o webdav-downloader.tar
```

完成后，项目目录里会生成一个 `webdav-downloader.tar` 文件（约 200MB）。

> 首次 build 需要下载 Python 基础镜像，视网络情况需 2–5 分钟。

---

## 第二步：在 NAS 上导入镜像

1. 打开极空间管理界面 → **Docker** → **镜像**
2. 点击**导入**，上传上一步生成的 `webdav-downloader.tar`
3. 等待导入完成，镜像列表中出现 `webdav-downloader:latest`

---

## 第三步：创建并启动容器

在极空间 Docker 管理界面创建容器，填写以下配置：

| 配置项 | 填写内容 |
|--------|----------|
| 镜像 | `webdav-downloader:latest` |
| 容器名称 | `webdav-downloader` |
| 端口映射 | 宿主机 `8765` → 容器 `8765` |
| 目录挂载 | NAS 上的下载目录 → `/downloads` |
| 环境变量 | `TZ` = `Asia/Shanghai` |
| 重启策略 | `unless-stopped` |

> **目录挂载示例**：如果你想把文件下载到 `/极空间/downloads`，则填 `/极空间/downloads` → `/downloads`。  
> 在极空间文件管理器里右键目录 → 属性，可以看到完整路径。

---

## 第四步：访问

在同一局域网的任意设备（电脑、手机）上，打开浏览器访问：

```
http://NAS的IP地址:8765
```

例如：`http://192.168.1.100:8765`

---

## 使用说明

1. 填入 WebDAV 服务器地址、用户名、密码，点击**连接**
2. 浏览文件，点击文件名可**预览**（图片 / 视频 / 音频 / PDF / 文本）
3. 勾选要下载的文件，「下载到」填 `/downloads`
4. 点击**下载选中**，底部队列实时显示进度和速度

> Docker 版没有弹出文件夹对话框，下载路径直接手动输入容器内路径（默认填 `/downloads` 即可）。

---

## 常见问题

**访问 8765 端口打不开？**  
检查极空间防火墙：控制台 → 安全性 → 防火墙，确认 8765 端口未被屏蔽。

**下载的文件找不到？**  
检查目录挂载是否正确，容器内 `/downloads` 对应 NAS 上你填的那个目录。

**build 时下载很慢？**  
在 Docker Desktop 设置里添加国内镜像加速源：  
Settings → Docker Engine，在 JSON 里加入：
```json
"registry-mirrors": ["https://dockerproxy.com", "https://mirror.baidubce.com"]
```

**想更新到新版本？**  
重新 build 一个新的 `webdav-downloader.tar`，在 NAS 上停止旧容器、删除旧镜像、重新导入新镜像、重新创建容器即可。

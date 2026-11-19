# 服务管理（dockerup）

飞牛 fnOS 第三方应用：可视化管理 fnOS 上的 Docker Compose 项目。

由一段「交互式升级 Shell 脚本」改造而来——扫描 `/vol1/1000/docker` 下的一级子目录，  
对选中的项目执行 `down → pull → up -d` 升级流程，并提供 Web 界面。

## 功能

- 自动扫描项目根目录下的一级子文件夹，识别 `docker-compose.yml / yaml`、`compose.yml / yaml`
- 展示每个项目的容器运行状态（运行数、容器名、运行时长）
- 单项目操作：升级（停止 → 拉取最新镜像 → 启动）、启动、停止、重启、查看日志
- 一键批量升级全部项目（串行执行，逐项输出日志）
- 升级完成后自动清理悬空镜像（旧版本镜像）
- 手动清理：工具栏「清理」按钮可删除所有已停止的容器（不影响数据卷和运行中的容器）
- 任务日志实时流式输出，可复制
- 项目根目录可在界面中修改，并自动探测常见位置（/vol1~8/*/docker）

## 安装

1. 打开飞牛 fnOS「应用中心」→ 手动安装 → 选择 `dockerup.fpk`
2. 安装完成后桌面会出现「服务管理」图标（名称取自 `app/ui/config` 的 `title`）
3. 首次打开如提示根目录不存在，点顶部「设置」修改为实际的 Docker 项目目录

## 技术说明

- 后端：`app/server/server.py`，仅使用 Python3 标准库，无任何 pip 依赖
- 启动：`cmd/main` 自动探测 `/usr/bin/python3`（或应用中心的 python312 运行时）启动服务
- 端口：固定 `39190`（在 `manifest` 与 `app/ui/config` 中同步修改）
- 权限：`run-as: root`。原因是需要访问 `/var/run/docker.sock` 执行 docker 命令，  
  并读写 `/vol1/*` 下的用户项目目录
- 数据：配置保存在应用数据目录（`@appdata/dockerup/settings.json`），卸载重装可保留

## 安全提示

- 服务监听 `0.0.0.0:39190`，局域网内任何人都可以打开该页面并触发升级操作，  
  仅建议在家庭内网环境使用
- API 对项目名做了严格校验（只允许根目录下的一级子目录，禁止路径穿越），  
  命令执行不经过 shell 拼接，无命令注入风险

## 修改后重新打包

```bash
cd dockerup
npm run build      # 优先使用项目内 .tools/fnpack（已下载），否则用系统 PATH 中的 fnpack
```

`fnpack` 官方下载地址格式为 `https://static2.fnnas.com/fnpack/fnpack-1.2.3-{os}-{arch}`
（如 `darwin-arm64`、`linux-amd64`）。换机器时下载对应版本放到 `.tools/fnpack` 并赋予可执行权限即可，
或直接安装到系统 PATH。项目内 `.tools/` 和 `*.fpk` 均已加入 `.gitignore`。

## 目录结构

```
dockerup/
├── app/
│   ├── server/server.py      # 后端服务（Python3 标准库）
│   └── ui/
│       ├── config            # 桌面入口（iframe + 端口）
│       ├── images/           # 入口图标
│       └── www/              # 前端页面（原生 HTML/CSS/JS）
├── cmd/                      # 生命周期脚本（main 负责 start/stop/status）
├── config/
│   ├── privilege             # 运行身份（root）
│   └── resource              # 资源声明
├── wizard/                   # 预留安装向导
├── manifest                  # 应用元数据
├── ICON.PNG / ICON_256.PNG
└── dockerup.fpk              # 打包产物
```

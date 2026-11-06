# 飞书文档快速浏览 v1.0

一款 Windows 桌面小工具：把飞书文档常驻在你的屏幕边缘，浏览器式多分页切换、
贴边自动收起、空闲半透明，既不打扰工作又能随手查看和编辑文档。

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![平台](https://img.shields.io/badge/platform-Windows-lightgrey)

## 功能特性

- **浏览器式分页**：在文档里点飞书链接，自动弹出新分页；每页独立加载实例，
  切换不丢滚动位置；最多 20 页，重启后原样恢复
- **贴边隐藏**：窗口拖到屏幕边缘自动吸附缩进，只留一条拉手，鼠标悬停即弹出
- **真实半透明**：空闲时整窗半透明（直接透出桌面），选中自动变实；
  透明度、两段动画时长（透明渐变 / 收起滑入）均可在设置里调节
- **托盘常驻**：不出现在任务栏和 Alt+Tab，只留托盘图标
  （左键 显示/隐藏，右键菜单含退出）
- **开机自启**：设置里一键开关，写入当前用户注册表 Run 键，静默无窗口启动
- **编辑直写**：编辑结果通过 lark CLI 写回飞书文档，与网页端能力一致

## 运行环境

- Windows 10 / 11
- Python 3.10+
- [飞书官方 lark CLI](https://github.com/larksuite/lark-cli)（文档读取/写回走它，需先登录）

## 安装与运行

```bash
pip install -r requirements.txt
cp config.example.json config.json   # 填入你的飞书文档链接
python app.py
```

Windows 下建议双击 `启动工作日志贴.vbs`（无命令行窗口静默启动）。

### 打包成单文件 exe

```bat
pip install pyinstaller
build_exe.bat            :: 产物：dist\飞书文档快速浏览器 v1.0.exe
```

打包后 `web/` 已内嵌进 exe；`config.json` 与 `data/`（登录态、缓存、日志）存放在
**`%APPDATA%\FeishuDocQuickBrowser\`**，exe 目录保持干净，exe 随便挪配置都跟着走。
旧版本（配置在 exe 旁边）首次运行会自动迁移到新位置。
首次运行会出现引导页：粘贴飞书文档链接，并在窗口内扫码登录一次，之后登录态保留。

> 前提：系统需有 WebView2 运行时（Windows 11 自带；Win10 一般随 Edge 安装过）。

## 配置说明

首次使用会生成 `config.json`：

| 字段 | 说明 |
|---|---|
| `doc_url` | 首页加载的飞书文档链接 |
| `alpha_in` / `alpha_out` | 选中 / 空置时的不透明度（%） |
| `anim_dock_ms` | 收起 / 弹出动画时长（0 关闭动画） |
| `anim_alpha_ms` | 透明度渐变时长 |
| `dock_side` / `dock_px` | 贴边方向与收起后留出的拉手宽度 |

其余（分页列表、标题缓存、开机自启）均可在界面内完成，无需手改配置。

## 项目结构

```
app.py        主程序：无边框窗口、贴边动画、托盘、本地 HTTP 服务、设置
bridge.py     飞书桥接：基于 lark CLI 读写文档（标题/待办/图片/缩略图）
web/          前端外壳：分页条、状态栏、设置面板（iframe 内嵌飞书文档页）
```

## 隐私说明

`data/`（含 WebView2 登录态）与 `config.json` 均在 `.gitignore` 中，
不会被提交到仓库。

## License

MIT

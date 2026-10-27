# Apple Music Handoff

读取 macOS 的 Apple Music 播放状态，并通过 iCloud 和快捷指令同步至 iPhone。

## 功能

- 获取当前歌曲、歌手、歌单和歌单内位置。
- 获取播放进度、暂停状态、随机播放和循环设置。
- 在 iPhone 上打开 Apple Music 时同步上述内容。

## 运行环境

- macOS
- Python 3
- iOS 18
- 两台设备登录相同的 iCloud 和 Apple Music 账号

## 文件

| 文件 | 用途 |
| --- | --- |
| `music_state.py` | Mac 主程序 |
| `capture.js` | 读取 Apple Music 状态的脚本 |
| `Resume Mac Music iOS18 v8.shortcut` | iPhone 快捷指令 |
| `Start.command` | 启动后台服务 |
| `Stop.command` | 停止后台服务 |

## 启动

在 iPhone 上导入本仓库的快捷指令，或者通过 https://www.icloud.com/shortcuts/710186c6dc874d93a4ff5e9b0b5dd2ed 安装，然后双击 `Start.command`，或者运行：

```sh
cd /Users/cyh/Workspace/AppleMusicHandoff
python3 ./music_state.py start --icloud --port 8766
```

## 停止

双击 `Stop.command`，或者运行：

```sh
cd /Users/cyh/Workspace/AppleMusicHandoff
python3 ./music_state.py stop
```

查看状态：

```sh
python3 music_state.py status
```

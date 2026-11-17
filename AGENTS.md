# 给改这个仓库的人（包括 AI）

## 铁律：改完就推

**任何一次改动，改完立刻 commit + push 到远端。** 别攒着——这台机器一重装，没推上去的东西就没了。

```powershell
# 仓库位置
cd $env:USERPROFILE\.dsh\vendor\dsh-wechat-bridge

# 一条命令提交+推送（改了什么写清楚）
powershell -File .\sync.ps1 "修了什么、为什么"
```

仓库里已经装了 **post-commit 钩子**：只要 `git commit`，它会自动在后台 `git push origin HEAD`，日志写到 `.git/push.log`。所以哪怕忘了手动推，提交那一刻就会推。

**先看钩子有没有生效**：

```powershell
git log origin/main..HEAD --oneline    # 空 = 已全部推上去了
cat .git/push.log                       # 看有没有推送报错
```

## 改完必须做的三件事

1. **语法检查**：`python -m py_compile bridge/wechat_bridge.py`、`node --check lib/host.js`
2. **AST 自检**（防止 `NameError` 把整条链路弄哑）：扫描「用了但没定义」的变量。历史上已经因此挂过三次，每次都是所有消息静默。
3. **重启桥接**（改 Python 用 `POST /bridge/stop` + `/bridge/start`；改 host.js 还要 touch `~/.dsh/profiles/desktop/cordis.patch.yml`）

## 部署位置

| 位置 | 作用 |
|---|---|
| `~/.dsh/profiles/desktop/node_modules/dsh-wechat-bridge/` | **真正在跑的**（改这里才生效） |
| `~/.dsh/vendor/dsh-wechat-bridge/` | **git 仓库**（改了要同步过来再提交） |

两边要一致，否则提交的和你跑的不是一份代码。

## 不要碰的东西

- `bridge/bridge.log`（已在 .gitignore）
- `wechat-bridge-config.json` / `wechat-memory.json`（运行时数据，不在仓库里，含密钥和隐私）

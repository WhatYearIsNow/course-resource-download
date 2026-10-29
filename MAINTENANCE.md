# 维护、验证与版本切换

环境：Python 3.10+；浏览器与验证码 extras 仍按需安装。本项目按需执行，没有需要新建的常驻 Web 服务或固定业务端口。

## 开发入口

阅读顺序：SKILL.md/README → src/course_resource_download/cli.py → manifests/download/validation 模块 → tests/。

```text
python -m venv .venv
# 使用此虚拟环境的 Python，保留已有环境，不在系统 Python 中安装开发依赖
python -m pip install -r requirements-dev.txt
```

开发源码与正式安装目录分开，不能将安装目录链接到正在编辑的仓库。预览使用临时目录和合成数据；真实账号、密钥、profile、课程与运行状态不进入测试或仓库。

## 统一规则与检查

UTF-8、LF；Python 4 空格、JS/JSON/YAML 2 空格，沿用文件内已有命名。Markdown 的有意空格与换行保留。格式工具固定版本，依赖清单/锁文件随工具升级一起审阅。Ruff 只做格式化，不自动修复 lint 或排序导入；Prettier 不排序导入。

```text
python -m ruff format --check src scripts tests
python -m ruff check .
python -m coverage run -m pytest
python -m coverage report
```

格式修复单独运行并单独提交：

```text
python -m ruff format src scripts tests
```

排除生成物、第三方许可/源码、原始 fixtures、运行数据、日志和缓存；模板通过原流程构建。注释解释约束、单位和副作用，避免逐句重述。清单认领、权限判断、原子下载、OS 锁和退出码保持原行为；格式修复只运行 ruff format，不自动排序导入。

## 候选、发布与健康检查

从干净的确定提交构建到新的目录，先运行检查和测试，记录 Git SHA、工具版本、逐文件/产物哈希，再解包到临时目录复验。

```text
python -m build --sdist --wheel --outdir <新的产物目录>
python scripts/package_skill.py --out <新的.skill路径>
python scripts/release_check.py <产物目录>
```

以上仅创建本地候选。对外发布使用新的版本和标签，不覆盖已发布标签/附件；GitHub Release、包索引上传与安装切换分别执行，不把 CI push 当成自动上线。

离线健康检查：

```text
python -m course_resource_download --help
```

健康检查只验证本地入口和代码完整性，不代表当前真实网站可用或结果正确。Python 包在新虚拟环境安装构建产物，Node 仅安装生产依赖，再从项目外验证入口。不得用发送消息、提交作业、生图或真实课程写入充当健康检查。

## 安装和回退

先停止本项目运行中的任务，备份旧安装和私有配置，校验新包。暂存目录通过离线检查后再切换，保留旧版本及切换回执；失败时恢复旧代码，不能修改其他项目或共享浏览器服务。

本机统一工作区的 skill-workflow/skill-install 工具提供提交快照、候选回执、沙箱演练与恢复入口；它们不属于本项目运行依赖。各仓独立使用时仍可按上述原生命令验证和构建，不能假定本机私有编排脚本在公开包中存在。

_discovery.json、_manifest.json、下载文件与 .download.lock 是运行状态；不要手动删锁，不回退清单覆盖新下载认领。格式变更单独做副本迁移与校验。

回滚必须分别处理代码与数据。确认旧代码可读取当前数据后，保留最新数据再恢复代码；不兼容时停止切换，另做只读备份、迁移与恢复演练，不能用 git reset 或旧数据覆盖代替兼容处理。

## 分支与 CI

从实际默认分支创建 codex/ 前缀分支；格式、结构与发布工具改动分开提交。PR 写明影响、实际验证和未验证项；现有离线 CI 加入格式检查，保留已有支持矩阵。CI 没有真实站点凭据，不自动发布。分支保护/必需检查由仓库设置启用，文档约定不等于已经强制执行。

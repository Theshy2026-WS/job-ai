# Job AI — 手动求职邮件通知

运行 `python run.py`：读取本地简历，搜索直接雇主的官方招聘页与公开 ATS，核验岗位、筛选匹配项，发送一封职位与官网链接汇总邮件，然后退出。

**不自动投递、不上传简历、不操作申请表、不启动定时任务、不使用 LinkedIn。** 简历、邮箱配置、SMTP 凭据、历史记录不提交到仓库。

## 安装与运行

支持 Python 3.11+。Windows PowerShell：

```powershell
python -m pip install -r requirements.txt
Copy-Item digest_config.example.json digest_config.json
```

1. 将简历放到 `resumes/resume.pdf`，或修改本地配置中的 `resume_path`。
2. 修改 `digest_config.json` 中的 `recipient`、发件账号 `email.username` 和 `email.sender`。这个配置文件已被 Git 忽略。
3. 简历须有可提取文本；当前经验解析识别 `Data Engineer with 8+ years ...` 这类摘要，无法识别时停止，不沿用陈旧技能。技能从 PDF 提取；不会把 PDF 内容当作指令执行。

首次配置 Gmail 发信：

```powershell
python run.py --configure-email
```

在本机隐藏输入 Gmail **应用专用密码**，不是普通密码。脚本先验证登录，再保存到 Windows 凭据管理器；此命令不发信。不要将密码写入源码或聊天。Google 账号需支持应用专用密码，说明见 [Google 官方帮助](https://support.google.com/accounts/answer/185833?hl=en)。也可通过 `JOB_DIGEST_SMTP_PASSWORD` 环境变量提供凭据。默认使用隐式 TLS SMTP（465）；其他发件服务须支持同一方式。

之后每次手动运行：

```powershell
python run.py
```

不会建立后台循环。已有聊天或浏览器中的 Gmail 登录不能代替本地脚本的发信凭据。

## 先预览

```powershell
python run.py --dry-run
```

仅搜索并写本地 HTML/JSON/文本预览，不发送邮件，不标记已通知。默认报告：`job_hunter/data/digest/latest.html`。

## 筛选与真实性核验

- 先根据简历匹配岗位，再审核公司；优先较大或有行业知名度的直接雇主，排除外包、中介及较小或未审核公司。公司优先级是可审核的人工目录分类，不声称已实时核实员工人数。
- 不再仅查原有48个招聘板：`employer_directory.json` 增加知名公司官网，运行时从官网真实链接发现 ATS，并用搜索引擎发现目录内公司的官网职位。目录可继续扩充；不会把未知公司搜索结果自动当作可靠雇主。每轮重新核实官网到具体 ATS 的真实链接，不猜 board token。
- 来源是公司官网及其官方 Greenhouse、Ashby、Lever 招聘接口，不使用 LinkedIn 或聚合转载链接。清单是已核实公司的子集，不是全美所有公司的完整索引。
- 美国职位；按纽约时区最近两个日历月内的真实发布/重发日期筛选。不使用创建、更新时间或发现时间冒充发布日期。日期不明的岗位不发送。
- 保留 H-1B transfer 场景：明确仅公民/绿卡、拒绝所需赞助的排除；未说明赞助的会标注待确认。此默认筛选有针对性，不代表适合每位求职者。
- 排除实习、兼职、合同、临时岗位；未说明用工类型可保留。经验上限为8年：必需最低经验超过8年的排除；8+或8–10年的最低门槛为8，可保留。优先项不视为硬要求，不把公司成立年限当求职年限，也不把总年限等同于具体技术年限。
- 重点匹配数据工程、数据平台、仓库、管道和分析工程；区分必需技能与优先项；技能有合理交集的岗位可发送，未列出的具体技能作为核对提示，而不是一律丢弃。列出技能交集及简历未列出的技能。匹配分用于排序，不保证满足每项资格。
- 逐条复查职位详情，跳至失效页、首页或 LinkedIn 的不发送。官网来源核验失败会在报告中列明，不冒称“该公司没有岗位”。
- 已通知岗位去重；发送成功才更新通知历史。若本地存在旧申请数据库，仅以只读方式排除已处理的申请。仓库本身不含申请数据库。

公开的官网职位不保证最终录用或招聘进度；报告保留来源、发布日期和链接核验信息供人工判断。

## 数据与邮件结果

- `latest.html` / `latest.txt`：本次邮件预览。
- `latest.json`：本次职位及核验证据。
- `notifications.sqlite3`：本地通知历史和 outbox；不要提交到仓库。

若 SMTP 在发信后断开，无法确定是否到达，脚本会保留 outbox 编号并阻止静默重发。检查邮箱后：

```powershell
python run.py --resolve-delivery 1 sent
# 或确认没收到：
python run.py --resolve-delivery 1 retry
```

以上命令只更新本地判断，不发信；随后再手动运行 `python run.py`。

## 离线测试

```powershell
python -m unittest discover -s job_hunter/tests
```

测试使用合成简历文本、临时数据库和模拟 SMTP；不会向真实邮箱发信，也不会提交职位申请。

## 更新已有安装

执行 `git pull` 后仍使用本地 `digest_config.json`、简历和凭据。旧配置在内存中自动采用第二版默认规则：8年上限、匹配阈值45和官网发现；不会覆盖邮箱、路径或密码。新配置可用 `rules_version: 2` 显式自定义阈值和 `discovery`。动态招聘页、访问限制或缺少真实发布日期可能导致来源无法核验；报告会列出错误，不能保证覆盖所有公司或全部职位。

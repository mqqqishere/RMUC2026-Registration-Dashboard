# RMUC 2026 Registration Dashboard

一个面向 RoboMaster RMUC 2026 志愿填报阶段的非官方开源分析工具。

这个仓库把轻流志愿抓取、公告规则调剂、晋级名额推演、志愿决策评估、瑞士轮概率模拟和静态可视化看板放在同一套流程里，方便队伍、观众和社区开发者一起复现当前盘面、讨论规则边界，并把结果直接发布到 GitHub Pages。

## 项目能做什么

- 按 [RoboMaster 官方公告 1910](https://www.robomaster.com/zh-CN/resource/pages/announcement/1910) 的口径模拟 RMUC 2026 区域赛志愿录取与调剂
- 直接抓取轻流分享看板，自动识别学校与赛区字段
- 在完整 96 队 roster 基础上补全未提交学校，给出当前快照下的赛区落点
- 计算综合实力榜、赛区强度、国赛线、复活线和边界队伍
- 为每所学校生成“三个志愿分别怎么填”的志愿决策面板
- 复刻 2025 赛制的抽签、瑞士轮与淘汰赛，并用 Monte Carlo 给出国赛/复活概率
- 生成 `docs/data.json`、`docs/data.csv` 和前端页面，可直接托管为静态站点

## 仓库结构

- [`allocator.py`](./allocator.py)：按公告规则实现志愿录取与调剂
- [`qingflow.py`](./qingflow.py)：轻流分享看板抓取器
- [`qualification.py`](./qualification.py)：完整 roster、综合实力与晋级名额模型
- [`swiss_simulation.py`](./swiss_simulation.py)：瑞士轮与淘汰赛概率模拟
- [`scripts/build_dashboard.py`](./scripts/build_dashboard.py)：构建看板数据
- [`scripts/serve_dashboard.py`](./scripts/serve_dashboard.py)：本地 HTTP 服务与刷新接口
- [`scripts/start_dashboard.py`](./scripts/start_dashboard.py)：一键启动本地看板
- [`docs/index.html`](./docs/index.html)：静态前端页面
- [`tests/`](./tests)：核心规则与前端模板冒烟测试

## 快速开始

建议使用 Python 3.11 或更高版本。

```bash
git clone https://github.com/ggllzz04/RMUC2026-Registration-Dashboard.git
cd RMUC2026-Registration-Dashboard

# 可选：某些环境下抓取 Qingflow 时更稳
python3 -m pip install certifi

# 生成最新看板数据
python3 scripts/build_dashboard.py

# 启动本地看板
python3 scripts/start_dashboard.py
```

如果你只想预览当前仓库里已有的 `docs/` 产物，不想触发一次线上刷新：

```bash
python3 scripts/start_dashboard.py --skip-initial-refresh
```

## 常用命令

```bash
# 直接查看轻流分享看板抓取结果
python3 qingflow.py https://qingflow.com/appView/e3bol1op1c02/shareView/e3bol20d1c02

# 单独跑一次志愿调剂模拟
python3 allocator.py \
  --url https://qingflow.com/appView/e3bol1op1c02/shareView/e3bol20d1c02 \
  --output result.csv

# 跑测试
python3 -m unittest discover -s tests
```

## 数据与模型说明

- `distances.csv`：根据官方公告附表整理的学校距离数据
- `robomaster_2026_teams.csv`：96 支队伍的基础 roster
- `ranks.csv`：积分榜与相关补充数据
- 轻流分享看板：作为实时志愿输入源

需要特别说明的是：

- “国赛名额”部分按公告规则精确计算
- “复活赛名额”“志愿最优建议”“瑞士轮概率”属于模型推演，用于交流参考，不代表官方结论
- 当前页面展示的是“当前快照下”的分析结果，不是对最终报名截止状态的承诺

## 自动构建与发布

仓库自带 [`.github/workflows/update.yml`](./.github/workflows/update.yml)：

- push 到 `main` 后自动构建
- 每 5 分钟自动刷新一次 `docs/data.json` 等产物
- 构建完成后可直接部署到 GitHub Pages

如果你准备公开仓库，建议在 GitHub Pages 设置里把 Source 设为 `GitHub Actions`。

## 适合谁使用

- 想复现当前 RMUC 2026 志愿盘面的队伍
- 想讨论调剂规则、晋级口径和赛区强弱的社区用户
- 想基于现有数据模型继续扩展可视化、模拟器或分析脚本的开发者

## 免责声明

- 本项目为非官方社区工具，与 DJI / RoboMaster 官方无关
- 所有录取、晋级和赛程相关结果请以官方公告为准
- 如果你准备正式开源，请先确认仓库中不包含敏感信息、私有数据或不宜公开传播的素材
- 当前仓库尚未附带 `LICENSE`，建议在公开发布前补充明确的开源许可证

## 贡献

欢迎通过 Issue 或 PR 一起校验规则实现、补充数据来源、修正文案和改进前端展示。

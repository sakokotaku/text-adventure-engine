# text-adventure-engine

起因很蠢：嫌订阅 AI 贵，想试试直接调 API 玩文字冒险省钱。折腾了几个月之后发现，API 算下来 100 回合的费用和订阅差不多。绕了一大圈，回到原点。

但引擎做出来了。

我是怎么做的？全程零代码。所有代码由 Claude 和 GPT-4 生成，我负责在两个 AI 之间传话——把 Claude 的意见给 GPT，把 GPT 的意见给 Claude。如果是群聊，它们早吵起来了。我的角色大概是项目经理，或者说，两个互相不服气的承包商中间的传话筒。

**核心问题是什么？**

做文字冒险引擎，最大的敌人不是剧情设计，是 AI 的两个根本缺陷：一是跨 session 失忆，每次新对话角色关系、称呼习惯全部归零；二是叙事跳跃，你刚说"我送你回家"，下一段它已经写你们在咖啡馆了。

**怎么解决的？**

对抗失忆：存档不是简单的对话记录，而是一份结构化的"关系快照"，有专门的字段记录已建立的默契和称呼习惯，并明确标注"不得重置为初见反应"。对抗跳跃：写了 SCENE LOCK 和 TIME LOCK 规则，除非玩家主动说"我离开"，否则禁止切换场景、禁止推进时间。这是踩坑踩出来的，不是提前设计的。

**模型横评**

顺手把主流模型都测了一遍：Claude / GPT-4 系列 prompt 遵循最好，但最贵；DeepSeek V3 第一个登场角色永远姓林或姓方，性格多样性完全没有；Qwen 3.5-27b 测试第一轮直接崩，空间逻辑失效；Kimi k2.5 输出信息太密集，叙事节奏完全不对，像在读说明书。

作为省钱方案，它失败了。作为技术尝试，它是完整的。项目已开源，欢迎路过的人参考。README 由 Claude 代写，这句话也是 Claude 写的。

---

## 初版 vs 现在

| 功能 | 初版 | 现在 |
|------|------|------|
| 代码结构 | 单文件 `main.py` | 模块化（`llm/` `storage/` `utils/` `prompt/`） |
| 规则系统 | 单个 `engine_rules.txt` | 8 个专项规则文件，硬约束独立成 `core_constraints.txt` |
| 存档 | 简单对话记录 | 三层记忆系统：NPC 档案 + 关系节点 + 滚动事件日志 |
| 场景控制 | SCENE LOCK 基础实现 | Scene anchor 截断 + 玩家输入 XML 标签隔离 |
| NPC 管理 | 无 | 完整 NPC 注册、渲染、权重出场系统 |
| 好感度 | 无 | 结构化好感度追踪，触发保护机制 |
| 张力系统 | 无 | 张力值持久化，触发突发事件 |
| 骰子判定 | 无 | `roll_system.txt` 结构化判定 |
| 界面 | 命令行 | 命令行 + GUI 可选 |
| 管理工具 | 无 | 管理员控制台（批量修改世界状态） |
| 测试 | 无 | 500+ 测试用例，启动时字段注册表验证 |

## 特性

- **结构化规则注入**：通过 `prompt/` 下的规则文件约束 GM 行为，包括叙事节奏、字数限制、场景切换、NPC 管理等
- **三层记忆系统**：NPC 知识档案（永久）+ 关系节点（永久/稀疏）+ 滚动事件日志（压缩归档）
- **状态持久化**：完整的存档/读档系统，支持增量 JSON patch 更新
- **好感/张力系统**：内置角色好感度追踪与叙事张力管理
- **GUI 界面**：可选图形界面（`gui.py`）
- **管理员控制台**：批量修改世界状态（`admin_console.py`）
- **测试套件**：500+ 测试用例，保障核心逻辑正确性

## 项目结构

```
text_adventure/
├── main.py                  # 主循环入口
├── gui.py                   # 图形界面
├── admin_console.py         # 管理员控制台
├── run.bat                  # 启动脚本（命令行）
├── run_gui.bat              # 启动脚本（GUI）
├── config.example.json      # 配置模板
├── llm/
│   └── provider.py          # LLM 接口层（OpenRouter）
├── prompt/
│   ├── builder.py           # System Prompt 构建器
│   ├── engine_rules.txt     # 引擎核心规则
│   ├── core_constraints.txt # 硬约束（禁止项）
│   ├── narrative_rules.txt  # 叙事规则
│   ├── npc_system.txt       # NPC 管理规则
│   ├── affection_system.txt # 好感度系统
│   ├── tension_system.txt   # 张力系统
│   ├── roll_system.txt      # 骰子/随机系统
│   ├── system_check.txt     # GM 自检规则
│   └── save_template.json   # 存档结构模板
├── storage/
│   ├── save_manager.py      # 存档管理
│   └── memory.py            # 记忆/压缩系统
├── utils/
│   └── logger.py            # 日志工具
└── tests/
    └── test_engine.py       # 测试套件
```

## 快速开始

### 1. 安装依赖

```bash
python setup.py
# 或
python setup_v2.py
```

### 2. 配置

复制配置模板并填入你的 API Key：

```bash
cp config.example.json config.json
```

编辑 `config.json`：

```json
{
  "api_key": "YOUR_API_KEY_HERE",
  "model": "anthropic/claude-sonnet-4-5",
  "base_url": "https://openrouter.ai/api/v1"
}
```

API Key 从 [OpenRouter](https://openrouter.ai) 获取。

### 3. 启动

命令行模式：
```bash
run.bat
# 或
python main.py
```

GUI 模式：
```bash
run_gui.bat
# 或
python gui.py
```

## 运行测试

```bash
pytest tests/test_engine.py
```

## 设计理念

引擎的核心目标是将人类玩家在直接使用云端 LLM 时需要隐式完成的认知工作（状态管理、规则执行、叙事纠偏）外化为显式的架构组件。

关键原则：
- **规则即行为约束**：规则文件使用明确的`禁止`语气，而非建议性描述
- **读写对称性**：写入存档的字段必须在渲染时被消费，字段注册表在启动时验证
- **叙事连续性**：通过 `completed_facts` 等机制保障跨轮次的事实一致性

## 注意事项

- `config.json` 包含 API Key，已加入 `.gitignore`，请勿提交
- `logs/` 和 `saves/` 目录包含游戏数据，已加入 `.gitignore`
- 叙事语言默认为中文

## License

MIT

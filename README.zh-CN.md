# IndexWitness

**哪些 SQLite 索引可以一起删除，同时保持工作负载的查询结果和工作量预算？**

IndexWitness 在数据库的内存副本上做反事实实验：穷举选定索引的保留组合，执行真实查询、比较结果指纹，再给出“保留索引数量 × 查询工作量”的 Pareto 前沿。

Python 3.10 及以上，运行时仅用标准库。无需数据库服务、账号或模型。

[English](README.md) · [方法与边界](docs/methodology.md) · [MIT 许可证](LICENSE)

## 要解决的实际问题

ORM 迁移运行几年后，数据库里常常累积很多索引。假设索引 A 和 B 都能支持同一条查询：单独删掉 A，B 可以接替；单独删掉 B，A 也可以接替。但如果把两份“可删除”结果直接合并，查询就可能退化成全表扫描。

IndexWitness 验证组合本身。它可以提供证据说明：保留 `orders_customer` 或 `orders_customer_total` 中任意一个都符合预算，但两个一起删会超过某条查询的预算。结果变化、执行失败和超出资源限制的配置也会留下诊断。

适合索引清理评审、嵌入式应用的数据库瘦身、删除索引的迁移验证。结论只覆盖本次快照、查询、参数、SQLite 运行时和预算。

## 先跑示例

直接在源码目录运行，无需安装：

```sh
git clone https://github.com/DrEli7/indexwitness.git
cd indexwitness
python examples/make_demo.py --directory .local/demo
python -m indexwitness .local/demo/shop.db .local/demo/workload.json
```

生成可独立打开的 HTML 报告：

```sh
python -m indexwitness .local/demo/shop.db .local/demo/workload.json --format html --output .local/demo/report.html
```

在浏览器打开 `.local/demo/report.html` 即可，报告本地生成，不依赖服务。再次执行时请换一个输出文件名；程序拒绝覆盖已有文件。

示例有四个候选索引，因此会尝试 16 个配置。其中 `idx_customer_a` 和 `idx_customer_b` 是可相互替代的客户索引；查看联合删除见证，就能理解为什么两个分别可删除的索引不能直接一起退役。具体计数和建议配置取决于本机 SQLite 运行时。

也可以从源码安装命令：

```sh
python -m pip install .
indexwitness .local/demo/shop.db .local/demo/workload.json
```

运行使用 Python 自带的 `sqlite3` 模块；可选的打包安装步骤使用 setuptools。

## 编写工作负载

保存 UTF-8 JSON 文件，例如 `workload.json`：

```json
{
  "version": 1,
  "candidate_indexes": ["orders_customer", "orders_customer_total", "orders_status"],
  "queries": [
    {
      "name": "customer-history",
      "sql": "SELECT id, total FROM orders WHERE customer_id = ? ORDER BY id",
      "params": [42],
      "compare": "ordered",
      "weight": 10,
      "max_step_ratio": 1.25
    },
    {
      "name": "open-order-total",
      "sql": "SELECT sum(total) FROM orders WHERE status = :status",
      "params": {"status": "open"},
      "compare": "bag",
      "weight": 1,
      "max_step_ratio": 1.1
    }
  ]
}
```

示例中的表和索引需要事先存在于数据库中。工具只验证已有索引的删除组合，不会生成新索引或改写 SQL。

| 字段 | 含义 | 默认值 |
|---|---|---|
| `version` | 工作负载版本，必须为整数 `1` | 必填 |
| `queries` | 1 至 64 个有名称的查询用例 | 必填 |
| `candidate_indexes` | 本次考虑删除的显式非唯一索引，其余索引固定保留 | 所有符合条件的显式非唯一索引 |
| `name` | 唯一用例名，最多 128 个字符 | 必填 |
| `sql` | 单条只读 SQL，最多 65,536 个字符 | 必填 |
| `params` | `?` 参数使用数组，命名参数使用对象 | `[]` |
| `compare` | `bag` 比较行及重复次数；`ordered` 还比较返回顺序 | `"bag"` |
| `weight` | 汇总工作量时使用的正权重 | `1` |
| `max_step_ratio` | 该查询相对基线允许的最大工作量比，不小于 `1` | `1.25` |

同一条 SQL 的不同参数分别写成不同用例，用不同名称覆盖热门用户、稀有值、常见值和空结果。权重可以表达调用频率或重要程度，但任何一条查询超出自己的比例预算，都不能被其他查询的改善抵消。

如果返回顺序属于应用契约，选择 `ordered`，并使用能够完全消除并列的 `ORDER BY`，例如最后加唯一 ID。无排序的 `LIMIT`、排序键并列、聚合中的裸列，可能随执行计划合法地改变结果。结果指纹变化提示你检查这些假设，不能据此断言 SQLite 出错。详见[结果比较](docs/methodology.md#result-comparison)。

输入文件最多 4 MiB；未知字段和重复 JSON 键会被拒绝，避免拼写错误悄悄改变实验。参数值必须是 SQLite 能表示的 JSON 标量；工具不会把数组或对象展开成 SQL。

## 命令和退出码

```sh
python -m indexwitness app.db workload.json --format markdown --output index-review.md
python -m indexwitness app.db workload.json --format json --output index-review.json
python -m indexwitness app.db workload.json --max-indexes 6 --max-callbacks 500000 --max-rows 20000 --timeout 15
```

| 参数 | 默认值 | 作用 |
|---|---|---|
| `--format` | `text` | `text`、`json`、`markdown` 或独立 `html` |
| `--output` | 标准输出 | 写入新文件，拒绝已有路径、输入文件及源库 WAL／SHM／journal 路径冲突 |
| `--max-indexes` | `8` | 候选索引数量上限，范围 0 至 12，穷举 `2^n` 个组合 |
| `--max-callbacks` | `2000000` | 单次查询允许的 progress callback 次数 |
| `--max-rows` | `100000` | 单次查询返回的最大行数 |
| `--timeout` | `30` | 包括复制和全部配置在内的整体实验截止秒数 |
| `--fail-on-removal` | 关闭 | 完整搜索发现可删除配置时返回 `1` |

`--max-callbacks` 实际统计的是 SQLite progress interval 设为 1 时的 `progress_callbacks`。SQLite 官方将间隔描述为近似值，复杂语句的准备阶段也可能触发回调。它不是精确的 `SQLITE_STMTSTATUS_VM_STEP`，也不是耗时、CPU 指令或 I/O 指标。报告采用 callback 名称，避免把近似代理指标误当成精确计数。[SQLite 官方说明](https://www.sqlite.org/c3ref/progress_handler.html)

搜索复杂度为指数级：8 个候选产生 256 个配置，12 个候选产生 4,096 个配置。索引很多时，显式指定一个小的 `candidate_indexes` 列表。选择超限会报错，不会悄悄抽样后宣称完成穷举。

引擎另将数据库快照限制为 256 MiB，每条查询的编码结果数据限制为 64 MiB，每行限制为 16 MiB；结果大小计算包含类型和长度编码。这些是数据大小边界，不是 Python 进程内存占用承诺；SQLite 工作内存、连接副本和报告证据还会产生额外开销。

| 退出码 | 含义 |
|---|---|
| `0` | 实验完成；未开启相应标志时，可删除发现只作为报告信息 |
| `1` | 开启了 `--fail-on-removal`，且完整搜索发现可行删除配置 |
| `2` | 输入错误、基线失败、快照大小超限、整体截止时间到达或搜索不完整，需要查看诊断 |
| `130` | 用户中断 |

在 CI 中，`--fail-on-removal` 可以作为索引评审入口，提醒维护者解释当前工作负载用不到的索引。它不代表已经验证全部应用行为。

## 怎么读报告

JSON 包含完整证据；HTML 提供可浏览的汇总和逐查询执行计划，text 和 Markdown 适合快速评审。各视图展示的信息包括：

- **基线**：保留全部选定索引时的执行计划、结果指纹和回调数。
- **每个组合**：保留与删除的索引，以及每条查询的执行结果；错误和结果变化也会记录。
- **可行性**：结果指纹一致，而且每条查询都满足自己的 `max_step_ratio`。
- **Pareto 前沿**：没有其他可行配置在候选索引数量和加权回调数上都不更差，且至少一项更好。
- **最差查询比例**：避免汇总改善掩盖某条查询的退化。

`weighted_callbacks` 是“各查询回调数 × 权重”的总和；`worst_callback_ratio` 是最大的单查询候选／基线比例。它们不测量节省空间、写入吞吐、能耗或生产延迟。保留索引数量只计算选定候选；固定索引始终存在。

建议配置先最小化保留的候选索引数量，再最小化加权回调数；前沿也保留了“多留几个索引、换取更少汇总工作量”的备选。工具不会自动应用建议。

“完成”表示每个选定子集都已在指定限制下尝试。候选查询执行出错，或达到回调、行数、返回值大小限制时，该配置保守地判为不可行；中断前的计数不会当作成功执行的完整成本。基线失败或整体截止时间到达时，分析返回 `2`，不产生建议。

不同可行配置是备选方案。不要把多份可删除列表直接求并集；并集本身也是一个需要验证的新配置。

## 源数据库保护和 SQL 范围

程序使用 SQLite URI `mode=ro` 打开源库，通过 backup API 取得一致的内存快照，包括 WAL 中可见的已提交数据。每个候选从这份快照重新建立副本，删除索引只发生在这些可丢弃的副本内。程序不会把迁移应用到源库。

所有 UNIQUE 索引、主键和约束索引，以及候选列表外的索引均固定保留。含虚拟表的数据库会被拒绝。引擎关闭 trusted schema，并用只读 authorizer 和确定性函数白名单约束查询。写操作、`ATTACH`、加载扩展、随机／时间函数和不支持的特性会被拒绝。

数据在本地处理，不向外传输。报告不包含 SQL 正文、参数值或结果行，但会包含查询名称、schema 标识符、执行计划和指纹；发布报告前仍需检查这些应用元数据。

## 与已有工具的关系

| 项目 | 已有定位 | IndexWitness 的侧重点 |
|---|---|---|
| [SQLite Expert](https://github.com/sqlite/sqlite/blob/master/ext/expert/README.md) | 结合查询特征和 planner 决策建议新索引 | 实际执行已有索引的联合删除实验 |
| [Litescope](https://github.com/croc100/Litescope) | SQLite 运维、诊断、迁移和索引建议 | 小范围、离线、每个选定子集都有证据的实验报告 |
| [RegreSQL](https://github.com/boringSQL/regresql) | PostgreSQL 查询结果和执行计划的回归验证 | SQLite 索引保留子集搜索及逐查询工作量预算 |

索引调优、回归验证和子集穷举都是已有技术。本项目把实际执行、结果比较和联合退役问题组合成一个小而实用的标准库工具，不声称首创索引分析，也不证明数据库的全局最优设计。

## 在采用结论前了解边界

- 证据只覆盖当前快照和参数，不能代替对隐藏查询、未来数据、罕见运维任务及不同 SQLite 版本的验证。
- 回调数是受控实验中的工作量代理；不同操作成本不等，磁盘、缓存和 Python 回调开销也会影响真实耗时。
- 工作负载只读，不测量 INSERT／UPDATE／DELETE 的收益，也不覆盖外键维护工作负载。
- SHA-256 指纹是概率性见证，不是数学等价证明；比较严格区分 SQLite 值类型，不对浮点数使用容差。
- 候选查询达到限制时判为不可行，不能假设它的完整结果一致或成本合格；基线失败或整体截止时间到达时，不给出建议。输入和执行上限也有意限制了大库及依赖扩展的数据库。
- 删除索引使页可被 SQLite 重用；要缩小文件通常还需单独维护。本工具不估算可释放的文件系统字节。

详细假设和公式见[方法说明](docs/methodology.md)。

## 开发

```sh
python -m unittest discover -s tests -v
```

测试使用标准库和临时数据库。欢迎补充资源边界、可复现实验、反例数据及报告解释。提交问题时请提供 Python／SQLite 版本、诊断和可公开的最小合成用例，避免上传私有数据库。

采用 [MIT 许可证](LICENSE)。

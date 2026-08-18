# Profile: design

> 游戏策划（Game Design）项目类型模板。
>
> 适用：剧情/世界观文案、玩法/系统设计、数值/配置表、关卡设计等策划职能的独立仓库。
> 产出物是设计规格（唯一真相来源），供实现侧（unity/unreal/game-server/backend）引用。

## 产出物形态

| 类型 | 位置 | 说明 |
|------|------|------|
| 设计文档 | `docs/domains/` + `docs/flows/` + `docs/design/` | 玩法/系统/世界观规格 |
| 数值配置表 | `tables/` | 数值表、技能表、掉落表、经济表 |
| 导出产物 | `output/`（generated_dirs） | 打包配置、生成文档 |

## SDD 类型

固定为 **docs 型**（`spec.type: docs`）——策划产出全部是规格文档，不涉及 contract/protocol。

## 关联

- 下游实现：`unity/`、`unreal/`、`game-server/`、`backend/`
- 引擎模板：见 [profiles/README.md](../README.md)

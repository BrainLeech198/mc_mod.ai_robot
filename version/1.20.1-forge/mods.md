# mods 清单（1.20.1-forge）

mod jar **不重复入库**，统一以 `D:\File\claude\mc_yzt\yztc` 为准（命名约定见该目录的 CLAUDE.md）。

## 装法

| 端 | 装什么 | 数量 |
|----|--------|------|
| **AI 客户端** | `yztc/客户端需要/` 全部（26 个）+ ControlMod（自研，后补） | 26 + 1 |
| **服务端** | `yztc/服务器全部/` 全部（36 个）+ OPAC（用户加装） | 36 + 1 |

`服务器全部` = `仅需服务器`(14) + `客户端需要`(26) 的并集，直接整目录上传即可。

## 客户端需要（26）

```
[GT车包] gtcraft-2.0.1(1.20.1).jar
[MrCrayfish 的家具：重制] refurbished_furniture-forge-1.20.1-1.0.20.jar
[Xaero的世界地图] xaeroworldmap-forge-1.20.1-1.40.11.jar
[Xaero的小地图] xaerominimap-forge-1.20.1-25.3.10.jar
[丰富的生态] ecologics-forge-1.20.1-2.2.7.jar
[云北城建] yunbeiuc-forge-1.0.0-26w32a-hotfix.jar
[创世神UI] WorldEditCUI-1.20+01.jar
[创世神] worldedit-mod-7.2.15.jar
[前置·被GT车包、官方包依赖] Immersive Vehicles-1.20.1-23.0.0.jar
[前置·被MrCrayfish家具依赖] framework-forge-1.20.1-0.8.0.jar
[前置·被多维度、创世神UI、沉浸式飞机、云北城建依赖] architectury-9.2.14-forge.jar
[前置·被砍树连锁依赖] collective-1.20.1-8.13.jar
[前置·被自然音效依赖] CreativeCore_FORGE_v2.12.35_mc1.20.1.jar
[前置·被静谧四季依赖] GlitchCore-forge-1.20.1-0.0.1.1.jar
[前置·被静谧四季修复依赖] SereneSeasons-forge-1.20.1-9.1.0.2.jar
[地图同步] mapsyncer-1.0.3-forge-1.20.1.jar
[席地而坐] sit-1.20.1-1.3.5.jar
[我的世界铁路] MTR-forge-4.0.3+1.20.1.jar
[机械动力] create-1.20.1-6.0.8.jar
[沉浸式车辆官方包] MTS Official Pack-1.20.1-V28.2.jar
[沉浸式飞机] immersive_aircraft-1.4.0+1.20.1-forge.jar
[渲染优化] embeddium-0.3.31+mc1.20.1.jar
[砍树连锁] treeharvester-1.20.1-9.1.jar
[自然音效] AmbientSounds_FORGE_v6.3.4_mc1.20.1.jar
[遥远地平线] DistantHorizons-2.4.5-b-1.20.1-fabric-forge.jar
[静谧四季修复] sereneseasonsfix-1.20.2-1.1.1.0.jar
```

## 关键 mod 对接入的意义

| mod | 用途 |
|-----|------|
| `[创世神] worldedit-mod-7.2.15.jar` | 建造主力，ControlMod 调其 Java API |
| `[创世神UI] WorldEditCUI` | 选区可视化（人看的，bot 不依赖） |
| `[Xaero的小地图/世界地图]` | 与 OPAC 领地显示集成 |
| `[渲染优化] embeddium`、`[遥远地平线]` | 客户端渲染，bot 跑无头时可留可关 |
| `[野粽乡·超级工具箱] yzt_super` | 仅服务端：登录 `/yzts`（客户端无需装） |

## 还差的 jar

- **ControlMod**（自研客户端模组，实现 AI 接口）—— 待开发
- **Open Parties and Claims** `forge-1.20.1-0.32.7` —— 服务端+客户端各一份，用户从 Modrinth 下载加装
  - https://modrinth.com/mod/open-parties-and-claims/versions?l=forge&g=1.20.1

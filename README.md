# astrbot_plugin_cumtelectricity

中国矿业大学宿舍剩余电量查询插件

## 指令

- `/电费 <宿舍号>`：临时查询指定宿舍当前剩余电量。
- `/电费 绑定 <宿舍号>`：把宿舍号绑定到当前 QQ 号。
- `/电费 楼栋列表`：查看当前电费接口返回的楼栋。
- `/电费`：查询当前 QQ 号已绑定宿舍的剩余电量。

## 配置

插件会通过 `_conf_schema.json` 在 AstrBot WebUI 中生成配置项。默认值：

- `area_id` / `area_name`: `1` / `中国矿业大学`
- `building_id` / `building_name`: `14` / `兰梅`
- `auto_detect_building`: `true`

使用前请自行更换`_conf_schema.json`中的`account`字段  
开启自动识别楼栋后，如果优先楼栋查不到房间，插件会自动获取楼栋列表并逐栋尝试。

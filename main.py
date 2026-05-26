from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register


ELECTRICITY_PATTERN = re.compile(r"剩余电量\s*[:：]?\s*(\d+(?:\.\d+)?)")
DECIMAL_PATTERN = re.compile(r"(\d+\.\d+)")


class ElectricityQueryError(Exception):
    """Raised when CUMT electricity service cannot return a usable reading."""

    def __init__(self, message: str, retcode: str | None = None):
        super().__init__(message)
        self.retcode = retcode


@dataclass(frozen=True)
class ElectricityBuilding:
    building_id: str
    building_name: str


@dataclass(frozen=True)
class ElectricityReading:
    remaining: float
    source_message: str
    building: ElectricityBuilding


@register("astrbot_plugin_cumtelectricity", "bobsers", "中国矿业大学宿舍电费查询", "1.2.0")
class CumtElectricityPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or {}
        self._building_cache: list[ElectricityBuilding] | None = None

    @filter.command("电费")
    async def electricity(
        self, event: AstrMessageEvent, action_or_room: str = "", room: str = ""
    ):
        """查询宿舍电费；支持 /电费 <宿舍号> 和 /电费 绑定 <宿舍号>。"""
        action_or_room = action_or_room.strip()
        room = room.strip()

        if action_or_room in {"帮助", "help", "?"}:
            yield event.plain_result(self._usage_text())
            return

        if action_or_room == "楼栋列表":
            try:
                buildings = await self._fetch_buildings(refresh=True)
            except ElectricityQueryError as exc:
                yield event.plain_result(f"楼栋列表获取失败：{exc}")
                return

            lines = ["当前电费接口可用楼栋："]
            lines.extend(
                f"{building.building_name}（{building.building_id}）"
                for building in buildings
            )
            yield event.plain_result("\n".join(lines))
            return

        if action_or_room == "绑定":
            if not room:
                yield event.plain_result("请发送：/电费 绑定 <宿舍号>")
                return

            try:
                room_number = self._normalize_room_number(room)
            except ValueError as exc:
                yield event.plain_result(f"绑定失败：{exc}")
                return

            await self.put_kv_data(self._binding_key(event), room_number)
            yield event.plain_result(f"已绑定宿舍号：{room_number}\n之后发送 /电费 即可查询本宿舍电费。")
            return

        try:
            room_number = await self._resolve_room_number(event, action_or_room)
        except ValueError as exc:
            yield event.plain_result(str(exc))
            return

        try:
            reading = await self._query_remaining_electricity(room_number)
        except ElectricityQueryError as exc:
            logger.warning(f"电费查询失败: {exc}")
            yield event.plain_result(f"电费查询失败：{exc}")
            return
        except Exception as exc:  # noqa: BLE001 - keep plugin failures user-visible but contained.
            logger.exception("电费查询出现未预期错误")
            yield event.plain_result(f"电费查询失败：{exc}")
            return

        queried_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        location = f"{reading.building.building_name} {room_number}".strip()
        lines = [
            f"{location} 当前剩余电量：{reading.remaining:g} 度",
            f"查询时间：{queried_at}",
        ]

        threshold = self._config_float("low_power_threshold", 20.0)
        if threshold > 0 and reading.remaining <= threshold:
            lines.append(f"提醒：电量已不高于 {threshold:g} 度，请及时充值。")

        yield event.plain_result("\n".join(lines))

    async def _resolve_room_number(
        self, event: AstrMessageEvent, action_or_room: str
    ) -> str:
        if action_or_room:
            return self._normalize_room_number(action_or_room)

        room_number = await self.get_kv_data(self._binding_key(event), "")
        if room_number:
            return self._normalize_room_number(str(room_number))

        raise ValueError(
            "你还没有绑定宿舍号。\n"
            "用法：\n"
            "/电费 <宿舍号>\n"
            "/电费 绑定 <宿舍号>\n"
            "绑定后可直接发送 /电费 查询本宿舍电费。"
        )

    async def _query_remaining_electricity(self, room_number: str) -> ElectricityReading:
        default_building = self._default_building()
        try:
            return await self._query_room_in_building(room_number, default_building)
        except ElectricityQueryError as exc:
            if exc.retcode != "60037" or not self._config_bool(
                "auto_detect_building", True
            ):
                raise
            first_error = exc

        buildings = await self._fetch_buildings()
        tried = [default_building.building_name]
        for building in buildings:
            if building == default_building:
                continue
            tried.append(building.building_name)
            try:
                return await self._query_room_in_building(room_number, building)
            except ElectricityQueryError as exc:
                if exc.retcode == "60037":
                    continue
                raise

        raise ElectricityQueryError(
            f"未在已知楼栋中找到房间 {room_number}。已尝试：{'、'.join(tried)}。"
            f"原始错误：{first_error}",
            retcode="60037",
        )

    async def _query_room_in_building(
        self, room_number: str, building: ElectricityBuilding
    ) -> ElectricityReading:
        data = await self._post_tsm(
            self._build_query_payload(room_number, building),
            "synjones.onecard.query.elec.roominfo",
        )
        return self._parse_roominfo_response(data, building)

    async def _fetch_buildings(self, refresh: bool = False) -> list[ElectricityBuilding]:
        if self._building_cache is not None and not refresh:
            return self._building_cache

        data = await self._post_tsm(
            {
                "query_elec_building": {
                    "aid": self._config_str("aid", "0030000000002501"),
                    "account": self._config_str("account", "179382"),
                    "area": self._area_payload(),
                }
            },
            "synjones.onecard.query.elec.building",
        )
        if not isinstance(data, dict):
            raise ElectricityQueryError("楼栋接口返回格式异常")

        result = data.get("query_elec_building")
        if not isinstance(result, dict):
            raise ElectricityQueryError("楼栋接口返回中缺少 query_elec_building")

        retcode = str(result.get("retcode", ""))
        if retcode != "0":
            message = str(result.get("errmsg", "未知错误")).strip()
            raise ElectricityQueryError(
                f"楼栋列表查询失败，返回码：{retcode or '无'}，错误信息：{message}",
                retcode=retcode,
            )

        buildings = []
        for item in result.get("buildingtab", []):
            if not isinstance(item, dict):
                continue
            building_id = str(item.get("buildingid", "")).strip()
            building_name = str(item.get("building", "")).strip()
            if building_id and building_name:
                buildings.append(ElectricityBuilding(building_id, building_name))

        if not buildings:
            raise ElectricityQueryError("楼栋列表为空")

        self._building_cache = buildings
        return buildings

    async def _post_tsm(self, jsondata: dict[str, Any], funname: str) -> Any:
        endpoint_url = self._config_str(
            "endpoint_url", "https://yktm.cumt.edu.cn/web/Common/Tsm.html"
        )
        timeout_seconds = self._config_float("timeout_seconds", 15.0)
        verify_tls = self._config_bool("verify_tls", True)

        form_data = {
            "jsondata": json.dumps(
                jsondata, ensure_ascii=False, separators=(",", ":")
            ),
            "funname": funname,
            "json": "true",
        }
        headers = {
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": "https://yktm.cumt.edu.cn",
            "Referer": endpoint_url,
            "User-Agent": self._config_str(
                "user_agent",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/142.0.0.0 Safari/537.36",
            ),
            "X-Requested-With": "XMLHttpRequest",
        }

        try:
            async with httpx.AsyncClient(
                timeout=timeout_seconds, verify=verify_tls, follow_redirects=True
            ) as client:
                response = await client.post(endpoint_url, data=form_data, headers=headers)
                response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise ElectricityQueryError(f"HTTP 错误：{exc.response.status_code}") from exc
        except httpx.RequestError as exc:
            raise ElectricityQueryError(f"请求失败：{exc}") from exc

        try:
            data = response.json()
        except ValueError as exc:
            preview = response.text.strip()[:120] or "空响应"
            raise ElectricityQueryError(f"JSON 解析失败：{preview}") from exc

        return data

    def _build_query_payload(
        self, room_number: str, building: ElectricityBuilding
    ) -> dict[str, Any]:
        return {
            "query_elec_roominfo": {
                "aid": self._config_str("aid", "0030000000002501"),
                "account": self._config_str("account", "179382"),
                "room": {
                    "roomid": room_number,
                    "room": room_number,
                },
                "floor": {
                    "floorid": self._config_str("floor_id", ""),
                    "floor": self._config_str("floor_name", ""),
                },
                "area": self._area_payload(),
                "building": {
                    "buildingid": building.building_id,
                    "building": building.building_name,
                },
            }
        }

    def _parse_roominfo_response(
        self, data: Any, building: ElectricityBuilding
    ) -> ElectricityReading:
        if not isinstance(data, dict):
            raise ElectricityQueryError("接口返回格式异常")

        result = data.get("query_elec_roominfo")
        if not isinstance(result, dict):
            raise ElectricityQueryError("接口返回中缺少 query_elec_roominfo")

        retcode = str(result.get("retcode", ""))
        message = str(result.get("errmsg", "")).strip()
        if retcode != "0":
            raise ElectricityQueryError(
                f"查询失败，返回码：{retcode or '无'}，错误信息：{message or '未知错误'}",
                retcode=retcode,
            )

        remaining = self._extract_remaining_electricity(message)
        if remaining is None:
            raise ElectricityQueryError(
                f"无法从接口响应中提取剩余电量：{message or '空错误信息'}"
            )

        return ElectricityReading(
            remaining=remaining, source_message=message, building=building
        )

    def _area_payload(self) -> dict[str, str]:
        return {
            "area": self._config_str("area_id", "1"),
            "areaname": self._config_str("area_name", "中国矿业大学"),
        }

    def _default_building(self) -> ElectricityBuilding:
        return ElectricityBuilding(
            self._config_str("building_id", "14"),
            self._config_str("building_name", "兰梅"),
        )

    @staticmethod
    def _extract_remaining_electricity(message: str) -> float | None:
        match = ELECTRICITY_PATTERN.search(message)
        if match:
            return float(match.group(1))

        match = DECIMAL_PATTERN.search(message)
        if match:
            return float(match.group(1))

        return None

    @staticmethod
    def _normalize_room_number(room_number: str) -> str:
        normalized = room_number.strip().upper()
        if not normalized:
            raise ValueError("宿舍号不能为空。")
        if any(char.isspace() for char in normalized):
            raise ValueError("宿舍号不能包含空格。")
        if len(normalized) > 64:
            raise ValueError("宿舍号过长。")
        return normalized

    @staticmethod
    def _binding_key(event: AstrMessageEvent) -> str:
        sender_id = event.get_sender_id() or event.unified_msg_origin
        return f"bound_room:{sender_id}"

    @staticmethod
    def _usage_text() -> str:
        return (
            "用法：\n"
            "/电费 <宿舍号>：临时查询指定宿舍电费\n"
            "/电费 绑定 <宿舍号>：把宿舍号绑定到当前 QQ 号\n"
            "/电费 楼栋列表：查看当前接口返回的楼栋\n"
            "/电费：查询已绑定宿舍电费"
        )

    def _config_str(self, key: str, default: str) -> str:
        value = self.config.get(key, default)
        if value is None:
            return default
        return str(value)

    def _config_float(self, key: str, default: float) -> float:
        value = self.config.get(key, default)
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _config_bool(self, key: str, default: bool) -> bool:
        value = self.config.get(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on", "y"}
        return bool(value)

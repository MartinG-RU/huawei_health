"""Sensors for the Huawei Health integration."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    UnitOfLength,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import SPORT_TYPES
from .coordinator import HuaweiHealthData
from .entity import HuaweiHealthEntity

UNIT_STEPS: str = "шаги"
#: "h" is what HA prints for UnitOfTime.HOURS in every language; the list here is
#: already Russian ("мин"), so the hour counter spells its own unit out.
UNIT_HOURS: str = "ч"
#: HA prints "kcal" for UnitOfEnergy.KILO_CALORIE, so the calorie counters spell
#: their own unit out too - and drop the ENERGY device class, which would otherwise
#: complain that "ккал" is not a unit it knows.
UNIT_KCAL: str = "ккал"
UNIT_WORKOUTS: str = "тренировки"
UNIT_BPM: str = "уд/мин"
UNIT_SCORE: str = "баллы"


@dataclass(kw_only=True)
class HuaweiSensorDescription(SensorEntityDescription):
    """A sensor plus how to pull its number out of the coordinator snapshot."""

    value: Callable[[HuaweiHealthData], Any] = lambda data: None
    attributes: Callable[[HuaweiHealthData], dict[str, Any]] = lambda data: {}


def day_value(metric: str, lookback: int = 0, scale: float = 1.0
              ) -> Callable[[HuaweiHealthData], Any]:
    """Read one per-day metric, falling back to the previous days if needed."""

    def get(data: HuaweiHealthData) -> Any:
        found = data.day(metric, lookback)
        return None if found is None else found[1] * scale

    return get


def day_date(metric: str, lookback: int = 0) -> Callable[[HuaweiHealthData], dict]:
    def attributes(data: HuaweiHealthData) -> dict[str, Any]:
        found = data.day(metric, lookback)
        return {} if found is None else {"date": found[0]}

    return attributes


def day_stamp(metric: str, lookback: int = 0
              ) -> Callable[[HuaweiHealthData], Any]:
    """Read a per-day metric that is already a datetime - no scaling applied."""

    def get(data: HuaweiHealthData) -> Any:
        found = data.day(metric, lookback)
        return None if found is None else found[1]

    return get


def session_value(key: str, scale: float = 1.0
                  ) -> Callable[[HuaweiHealthData], Any]:
    def get(data: HuaweiHealthData) -> Any:
        session = data.latest_session
        value = None if session is None else session.get(key)
        return None if value is None else value * scale

    return get


def session_attributes(data: HuaweiHealthData) -> dict[str, Any]:
    session = data.latest_session
    if not session:
        return {}
    attributes = {
        "start": session["start"].strftime("%Y-%m-%d %H:%M"),
        "end": session["end"].strftime("%Y-%m-%d %H:%M"),
        "duration_min": session["duration_min"],
        "distance_m": session["distance_m"],
        "calories_kcal": session["kcal"],
        "segments": session["segments"],
        "sport_type": session["sport_type"],
        "device_code": session["device_code"],
    }
    if session.get("record_source") == "motion_path":
        attributes["record_source"] = "motion_path"
        attributes["duration_seconds"] = session["duration_seconds"]
        attributes["calories_status"] = "unit_not_verified"
    if session.get("altitude_floor_samples"):
        attributes["altitude_floor_samples"] = session["altitude_floor_samples"]
    return attributes


def activity_name(data: HuaweiHealthData) -> str | None:
    session = data.latest_session
    if not session:
        return None
    # A state outside the declared options makes HA log a warning, so unknown ids from a
    # newer app version collapse to one value.
    if session.get("record_source") == "motion_path":
        return session.get("confirmed_label", "unknown")
    return SPORT_TYPES.get(session["sport_type"], "unknown")


def _hhmm(total_minutes: Any) -> str | None:
    """Minutes as a zero-padded ``HH:MM``, the way a watch reads it."""
    if total_minutes is None:
        return None
    try:
        total = int(round(float(total_minutes)))
    except (TypeError, ValueError):
        return None
    total = max(total, 0)
    return f"{total // 60:02d}:{total % 60:02d}"


def day_duration_clock(metric: str, lookback: int = 0
                       ) -> Callable[[HuaweiHealthData], str | None]:
    """A per-day minute metric as ``HH:MM``."""
    def get(data: HuaweiHealthData) -> str | None:
        found = data.day(metric, lookback_days=lookback)
        return None if not found else _hhmm(found[1])

    return get


def _duration_attributes(metric: str, lookback: int = 0
                         ) -> Callable[[HuaweiHealthData], dict[str, Any]]:
    """The raw minute count beside the clock face, so nothing is lost."""
    def attributes(data: HuaweiHealthData) -> dict[str, Any]:
        found = data.day(metric, lookback_days=lookback)
        if not found:
            return {}
        return {"minutes": found[1], "date": found[0]}

    return attributes


def day_clock(metric: str, lookback: int = 0) -> Callable[[HuaweiHealthData], str | None]:
    """The local "HH:MM" of a stamp metric, for a list that must read like a clock."""
    def get(data: HuaweiHealthData) -> str | None:
        found = data.day(metric, lookback_days=lookback)
        if not found or not isinstance(found[1], datetime):
            return None
        return _in_time_zone(found[1], data).strftime("%H:%M")

    return get


def _clock_attributes(metric: str, lookback: int = 0
                      ) -> Callable[[HuaweiHealthData], dict[str, Any]]:
    """The full stamp plus its date, so a tap answers "which night was that"."""
    def attributes(data: HuaweiHealthData) -> dict[str, Any]:
        found = data.day(metric, lookback_days=lookback)
        if not found or not isinstance(found[1], datetime):
            return {}
        local = _in_time_zone(found[1], data)
        return {
            "date": found[0],
            "local_datetime": local.strftime("%Y-%m-%d %H:%M"),
            "time_zone": str(local.tzinfo),
        }

    return attributes


def session_duration_clock(key: str = "duration_min"
                           ) -> Callable[[HuaweiHealthData], str | None]:
    """The latest workout's active duration as HH:MM:SS, without minute rounding."""
    def get(data: HuaweiHealthData) -> str | None:
        import math
        session = data.latest_session
        if not session:
            return None
        raw = session.get("duration_seconds")
        try:
            seconds = float(raw) if raw is not None else float(session.get(key)) * 60
            if not math.isfinite(seconds) or seconds < 0:
                return None
            total = int(round(seconds))
        except (TypeError, ValueError, OverflowError):
            return None
        return f"{total // 3600:02d}:{total // 60 % 60:02d}:{total % 60:02d}"

    return get


def _days_left(data: HuaweiHealthData) -> float | None:
    deadline = data.tokens.refresh_expires_at
    return None if not deadline else round((deadline - time.time()) / 86400, 1)


def _in_time_zone(moment: datetime, data: HuaweiHealthData) -> datetime:
    """Convert a UTC stamp to the time zone Home Assistant is configured with."""
    try:
        config = getattr(getattr(data.coordinator, "hass", None), "config", None)
        name = getattr(config, "time_zone", None)
        if name:
            return moment.astimezone(ZoneInfo(name))
    except (KeyError, ValueError, TypeError):
        pass
    return moment.astimezone()  # container-local fallback


def _night_attributes(metric: str,
                      duration_metric: str) -> Callable[[HuaweiHealthData], dict]:
    """Attribute a sleep bound to the night it belongs to.

    Both bounds are read with the same one-day lookback as the other sleep sensors, so
    they describe the night that just ended. ``date`` names that night's day (the day the
    band woke up on) and ``sleep_duration`` repeats the night's length so a card can show
    the whole night without crossing four entities.
    """

    def attributes(data: HuaweiHealthData) -> dict[str, Any]:
        found = data.day(metric, lookback_days=1)
        if found is None:
            return {}
        night, moment = found
        result: dict[str, Any] = {"date": night}
        duration = data.day(duration_metric, lookback_days=1)
        if duration is not None:
            result["sleep_duration"] = duration[1]
        # Home Assistant renders a timestamp sensor as "22 hours ago", so the
        # clock time a dashboard card actually wants travels as an attribute.
        if isinstance(moment, datetime):
            local = _in_time_zone(moment, data)
            result["local_time"] = local.strftime("%H:%M")
            result["local_datetime"] = local.strftime("%Y-%m-%d %H:%M")
            result["time_zone"] = str(local.tzinfo)
        return result

    return attributes


def _token_dates(data: HuaweiHealthData) -> dict[str, Any]:
    def stamp(seconds: float) -> str:
        return datetime.fromtimestamp(seconds).strftime("%Y-%m-%d %H:%M")

    return {
        "access_token_expires": stamp(data.tokens.expires_at),
        "refresh_token_expires": stamp(data.tokens.refresh_expires_at),
    }


DESCRIPTIONS: tuple[HuaweiSensorDescription, ...] = (
    # ── the day's totals, matching the Health app's activity rings ─────────
    HuaweiSensorDescription(
        key="steps", translation_key="steps", native_unit_of_measurement=UNIT_STEPS,
        state_class=SensorStateClass.MEASUREMENT, icon="mdi:walk",
        value=day_value("steps"), attributes=day_date("steps")),
    HuaweiSensorDescription(
        key="distance", translation_key="distance", device_class=SensorDeviceClass.DISTANCE,
        native_unit_of_measurement=UnitOfLength.KILOMETERS,
        suggested_display_precision=2, state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:map-marker-distance",
        value=day_value("distance_m", scale=1 / 1000), attributes=day_date("distance_m")),
    HuaweiSensorDescription(
        key="calories", translation_key="calories",
        native_unit_of_measurement=UNIT_KCAL,
        suggested_display_precision=0, state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:fire",
        value=day_value("kcal"), attributes=day_date("kcal")),
    HuaweiSensorDescription(
        key="activity_duration", translation_key="activity_duration", device_class=SensorDeviceClass.DURATION, native_unit_of_measurement=UnitOfTime.MINUTES,
        entity_registry_enabled_default=False,
        state_class=SensorStateClass.MEASUREMENT, icon="mdi:timer-outline",
        value=day_value("duration_min"), attributes=day_date("duration_min")),
    HuaweiSensorDescription(
        key="walking_duration", translation_key="walking_duration", device_class=SensorDeviceClass.DURATION, native_unit_of_measurement=UnitOfTime.MINUTES,
        entity_registry_enabled_default=False,
        state_class=SensorStateClass.MEASUREMENT, icon="mdi:shoe-print",
        value=day_value("walk_min"), attributes=day_date("walk_min")),
    HuaweiSensorDescription(
        key="activity_duration_clock", translation_key="activity_duration_clock",
        icon="mdi:timer-outline",
        value=day_duration_clock("duration_min"), attributes=day_date("duration_min")),
    HuaweiSensorDescription(
        key="walking_duration_clock", translation_key="walking_duration_clock",
        icon="mdi:shoe-print",
        value=day_duration_clock("walk_min"), attributes=day_date("walk_min")),
    HuaweiSensorDescription(
        key="active_hours", translation_key="active_hours", native_unit_of_measurement=UNIT_HOURS, state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:clock-outline",
        value=day_value("active_hours"), attributes=day_date("active_hours")),
    HuaweiSensorDescription(
        key="moderate_high_intensity_minutes",
        translation_key="moderate_high_intensity_minutes", device_class=SensorDeviceClass.DURATION, native_unit_of_measurement=UnitOfTime.MINUTES,
        state_class=SensorStateClass.MEASUREMENT, icon="mdi:run-fast",
        value=day_value("moderate_high_intensity_minutes"),
        attributes=day_date("moderate_high_intensity_minutes")),
    HuaweiSensorDescription(
        key="step_goal", translation_key="step_goal", native_unit_of_measurement=UNIT_STEPS,
        icon="mdi:target-variant", entity_registry_enabled_default=False,
        value=day_value("step_goal"), attributes=day_date("step_goal")),
    # ── heart rate ─────────────────────────────────────────────────────────
    HuaweiSensorDescription(
        key="resting_heart_rate", translation_key="resting_heart_rate", native_unit_of_measurement=UNIT_BPM, state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:heart",
        value=day_value("resting_heart_rate"),
        attributes=day_date("resting_heart_rate")),
    HuaweiSensorDescription(
        key="heart_rate", translation_key="heart_rate", native_unit_of_measurement=UNIT_BPM, state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:heart-pulse",
        value=day_value("heart_rate"), attributes=day_date("heart_rate")),
    HuaweiSensorDescription(
        key="max_heart_rate", translation_key="max_heart_rate", native_unit_of_measurement=UNIT_BPM, state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:heart-plus", 
        value=day_value("max_heart_rate"), attributes=day_date("max_heart_rate")),
    HuaweiSensorDescription(
        key="min_heart_rate", translation_key="min_heart_rate", native_unit_of_measurement=UNIT_BPM, state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:heart-minus", 
        value=day_value("min_heart_rate"), attributes=day_date("min_heart_rate")),
    # ── sleep: the night that just ended is stamped with the day it woke up in ──
    HuaweiSensorDescription(
        key="sleep_duration", translation_key="sleep_duration", device_class=SensorDeviceClass.DURATION, native_unit_of_measurement=UnitOfTime.MINUTES,
        entity_registry_enabled_default=False,
        state_class=SensorStateClass.MEASUREMENT, icon="mdi:sleep",
        value=day_value("sleep_duration", lookback=1),
        attributes=day_date("sleep_duration", lookback=1)),
    HuaweiSensorDescription(
        key="sleep_deep", translation_key="sleep_deep", device_class=SensorDeviceClass.DURATION, native_unit_of_measurement=UnitOfTime.MINUTES,
        entity_registry_enabled_default=False,
        state_class=SensorStateClass.MEASUREMENT, icon="mdi:weather-night",
        value=day_value("sleep_deep", lookback=1),
        attributes=day_date("sleep_deep", lookback=1)),
    HuaweiSensorDescription(
        key="sleep_rem", translation_key="sleep_rem", device_class=SensorDeviceClass.DURATION, native_unit_of_measurement=UnitOfTime.MINUTES,
        entity_registry_enabled_default=False,
        state_class=SensorStateClass.MEASUREMENT, icon="mdi:brain",
        value=day_value("sleep_rem", lookback=1),
        attributes=day_date("sleep_rem", lookback=1)),
    HuaweiSensorDescription(
        key="sleep_light", translation_key="sleep_light", device_class=SensorDeviceClass.DURATION, native_unit_of_measurement=UnitOfTime.MINUTES,
        state_class=SensorStateClass.MEASUREMENT, icon="mdi:weather-night",
        entity_registry_enabled_default=False,
        value=day_value("sleep_light", lookback=1),
        attributes=day_date("sleep_light", lookback=1)),
    HuaweiSensorDescription(
        key="sleep_awake", translation_key="sleep_awake", device_class=SensorDeviceClass.DURATION, native_unit_of_measurement=UnitOfTime.MINUTES,
        state_class=SensorStateClass.MEASUREMENT, icon="mdi:eye",
        entity_registry_enabled_default=False,
        value=day_value("sleep_awake", lookback=1),
        attributes=day_date("sleep_awake", lookback=1)),
    HuaweiSensorDescription(
        key="sleep_score", translation_key="sleep_score", native_unit_of_measurement=UNIT_SCORE, state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:star-four-points-circle",
        value=day_value("sleep_score", lookback=1),
        attributes=day_date("sleep_score", lookback=1)),
    HuaweiSensorDescription(
        key="sleep_efficiency", translation_key="sleep_efficiency", native_unit_of_measurement=PERCENTAGE, state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:progress-clock", 
        value=day_value("sleep_efficiency", lookback=1),
        attributes=day_date("sleep_efficiency", lookback=1)),
    HuaweiSensorDescription(
        key="sleep_hrv", translation_key="sleep_hrv", native_unit_of_measurement=UnitOfTime.MILLISECONDS,
        entity_registry_enabled_default=False,
        state_class=SensorStateClass.MEASUREMENT, icon="mdi:waveform",
        value=day_value("sleep_hrv", lookback=1),
        attributes=day_date("sleep_hrv", lookback=1)),
    HuaweiSensorDescription(
        key="sleep_spo2", translation_key="sleep_spo2", native_unit_of_measurement=PERCENTAGE, state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
        icon="mdi:oximeter",
        value=day_value("sleep_spo2", lookback=1),
        attributes=day_date("sleep_spo2", lookback=1)),
    # The two ends of the night, as epoch stamps the cloud reports per sleep record.
    # TIMESTAMP needs no state_class and no unit; HA renders it in the user's zone.
    HuaweiSensorDescription(
        key="fall_asleep", translation_key="fall_asleep", device_class=SensorDeviceClass.TIMESTAMP, icon="mdi:sleep",
        entity_registry_enabled_default=False,
        value=day_stamp("fall_asleep", lookback=1),
        attributes=_night_attributes("fall_asleep", "sleep_duration")),
    HuaweiSensorDescription(
        key="wakeup", translation_key="wakeup", device_class=SensorDeviceClass.TIMESTAMP, icon="mdi:alarm",
        entity_registry_enabled_default=False,
        value=day_stamp("wakeup", lookback=1),
        attributes=_night_attributes("wakeup", "sleep_duration")),
    # The clock face of every sleep counter: a string state is the only thing HA
    # shows verbatim in the entity list.
    HuaweiSensorDescription(
        key="sleep_duration_clock", translation_key="sleep_duration_clock", icon="mdi:sleep",
        value=day_duration_clock("sleep_duration", lookback=1),
        attributes=_duration_attributes("sleep_duration", lookback=1)),
    HuaweiSensorDescription(
        key="sleep_deep_clock", translation_key="sleep_deep_clock", icon="mdi:weather-night",
        value=day_duration_clock("sleep_deep", lookback=1),
        attributes=_duration_attributes("sleep_deep", lookback=1)),
    HuaweiSensorDescription(
        key="sleep_rem_clock", translation_key="sleep_rem_clock", icon="mdi:head-snowflake",
        value=day_duration_clock("sleep_rem", lookback=1),
        attributes=_duration_attributes("sleep_rem", lookback=1)),
    HuaweiSensorDescription(
        key="sleep_light_clock", translation_key="sleep_light_clock", icon="mdi:weather-night-partly-cloudy",
        value=day_duration_clock("sleep_light", lookback=1),
        attributes=_duration_attributes("sleep_light", lookback=1)),
    HuaweiSensorDescription(
        key="sleep_awake_clock", translation_key="sleep_awake_clock", icon="mdi:eye",
        value=day_duration_clock("sleep_awake", lookback=1),
        attributes=_duration_attributes("sleep_awake", lookback=1)),
    # The two ends of the night as a clock face.
    HuaweiSensorDescription(
        key="fall_asleep_clock", translation_key="fall_asleep_clock", icon="mdi:sleep",
        value=day_clock("fall_asleep", lookback=1),
        attributes=_clock_attributes("fall_asleep", lookback=1)),
    HuaweiSensorDescription(
        key="wakeup_clock", translation_key="wakeup_clock", icon="mdi:alarm",
        value=day_clock("wakeup", lookback=1),
        attributes=_clock_attributes("wakeup", lookback=1)),
    # ── stress ─────────────────────────────────────────────────────────────
    HuaweiSensorDescription(
        key="stress", translation_key="stress", native_unit_of_measurement=UNIT_SCORE,
        entity_registry_enabled_default=False,
        state_class=SensorStateClass.MEASUREMENT, icon="mdi:gauge",
        value=day_value("stress_average"), attributes=day_date("stress_average")),
    HuaweiSensorDescription(
        key="stress_last", translation_key="stress_last", native_unit_of_measurement=UNIT_SCORE, state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:gauge", entity_registry_enabled_default=False,
        value=day_value("stress_last"), attributes=day_date("stress_last")),
    # ── workouts, rebuilt from the per-minute segments ─────────────────────
    HuaweiSensorDescription(
        key="last_activity", translation_key="last_activity", device_class=SensorDeviceClass.ENUM,
        options=list(SPORT_TYPES.values()),
        icon="mdi:run", value=activity_name, attributes=session_attributes),
    HuaweiSensorDescription(
        key="last_activity_duration", translation_key="last_activity_duration", device_class=SensorDeviceClass.DURATION, native_unit_of_measurement=UnitOfTime.MINUTES,
        entity_registry_enabled_default=False,
        state_class=SensorStateClass.MEASUREMENT, icon="mdi:timer",
        value=session_value("duration_min"), attributes=session_attributes),
    HuaweiSensorDescription(
        key="last_activity_duration_clock", translation_key="last_activity_duration_clock",
        icon="mdi:timer", value=session_duration_clock("duration_min"),
        attributes=session_attributes),
    HuaweiSensorDescription(
        key="last_activity_distance", translation_key="last_activity_distance", device_class=SensorDeviceClass.DISTANCE,
        native_unit_of_measurement=UnitOfLength.KILOMETERS,
        suggested_display_precision=2, state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:map-marker-distance",
        value=session_value("distance_m", scale=1 / 1000),
        attributes=session_attributes),
    HuaweiSensorDescription(
        key="last_activity_calories", translation_key="last_activity_calories",
        native_unit_of_measurement=UNIT_KCAL,
        suggested_display_precision=0, state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:fire", value=session_value("kcal"), attributes=session_attributes),
    HuaweiSensorDescription(
        key="workouts", translation_key="workouts", native_unit_of_measurement=UNIT_WORKOUTS,
        state_class=SensorStateClass.MEASUREMENT, icon="mdi:trophy-outline",
        value=lambda data: len(data.sessions),
        attributes=lambda data: {
            "window_days": (data.coordinator.data or {}).get("window_days")}),
    # ── session health: the one number that says when a login is due again ──
    HuaweiSensorDescription(
        key="relogin_in", translation_key="relogin_in", native_unit_of_measurement=UnitOfTime.DAYS, state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:account-clock", suggested_display_precision=0,
        value=_days_left, attributes=_token_dates),
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    data: HuaweiHealthData = entry.runtime_data
    async_add_entities(HuaweiHealthSensor(description, data)
                       for description in DESCRIPTIONS)


class HuaweiHealthSensor(HuaweiHealthEntity, SensorEntity):
    entity_description: HuaweiSensorDescription

    def __init__(self, description: HuaweiSensorDescription,
                 data: HuaweiHealthData) -> None:
        super().__init__(data)
        self.entity_description = description
        self._attr_unique_id = f"{data.entry.entry_id}_{description.key}"
        # translation_key comes from the description, so names live in translations/

    @property
    def native_value(self) -> Any:
        return self.entity_description.value(self._data)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self.entity_description.attributes(self._data)

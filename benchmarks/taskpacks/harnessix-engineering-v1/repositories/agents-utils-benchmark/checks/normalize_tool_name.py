from src.agent_utils import normalize_tool_name

assert normalize_tool_name("GetWeather") == "getweather"
assert normalize_tool_name("Get Weather!") == "get_weather_"

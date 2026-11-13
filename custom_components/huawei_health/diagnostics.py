"""Offline installation diagnostics; no credentials, coordinates or cloud calls."""
from pathlib import Path
import hashlib
EXPECTED = {'coordinator.py': '056cf02f5329d555826cf755b3fa95587f98f06ca262735832b2a9be415d269a', 'gps_export.py': '57de39531706222a64ef7e11f7fa14d89b93db5418ed1f1772cfcd6b9eaef074', '__init__.py': '9a69c15ef5f8c42354af1a61f602fc589693a888a8c26d65beb9d89c86c2ec34', 'manifest.json': 'fae112641437a8decb8682b51c3657e3379d0b9453857840ae759fe9b7c35654', 'const.py': '0685a3ae5a160fc498154e8122505b4d4ab63ab8f9aac2237d6348e06dd2adb1', 'workout_export.py': '8e59ad996f221af549cffc2a54cd13a62caf52836baafafd5f4117f01df78a45', 'entity.py': 'd09029aa449c97096d55cf3ccce51ea6326ef74168aae556d0b7708d978f1b05', 'motion_workouts.py': '4ad44a23452186116919055e1bafed24674f07eae95a058ac300b2d7f5751cdc', 'client.py': 'fb9d53fa11d2d6210aa3105e3ae22150befe008bc2ade4e1b1fcf7ea4845dfe9', 'sensor.py': '9259182ae2e7fc30fdcbaf81e69bc650e997a9c03d1206bdbc961b5544ab9e5b', 'services.yaml': 'eb19e992b279bd7c08fd7bf33c67f16d6b48f629980132e5a90b0dc30be45100', 'config_flow.py': '5b42578b5b54b348a88127ed0e03a3e529da89bbea5b13c43c6817e50aa7f8c4', 'sport_mapping.json': '0060ce25d4e5b78d8c679580481eeb703886a4927724853602004c706f0cb59b'}
def check_files():
    root=Path(__file__).parent
    return {name: {'exists': (root/name).is_file(), 'matches_expected_patch': (root/name).is_file() and hashlib.sha256((root/name).read_bytes()).hexdigest()==digest} for name,digest in EXPECTED.items()}
async def async_get_config_entry_diagnostics(hass, entry):
    return {'network_requests_attempted': False, 'runtime_client_present': getattr(getattr(entry,'runtime_data',None),'client',None) is not None, 'files': await hass.async_add_executor_job(check_files)}

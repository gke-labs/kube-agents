import sys

path = "/opt/hermes/tools/send_message_tool.py"
content = open(path).read()

target_re = '_TELEGRAM_TOPIC_TARGET_RE = re.compile(r"^\\s*(-?\\d+)(?::(\\d+))?\\s*$")'
patch_re = '\n_GOOGLE_CHAT_TARGET_RE = re.compile(r"^\\s*(spaces/[A-Za-z0-9_-]+)(?::(spaces/[A-Za-z0-9_-]+/threads/[A-Za-z0-9_-]+))?\\s*$")\n'

target_parse = 'def _parse_target_ref(platform_name: str, target_ref: str):\n    """Parse a tool target into chat_id/thread_id and whether it is explicit."""'
patch_parse = '\n    if platform_name == "google_chat":\n        match = _GOOGLE_CHAT_TARGET_RE.fullmatch(target_ref)\n        if match:\n            return match.group(1), match.group(2), True\n'

if target_re in content and target_parse in content:
    new_content = content.replace(target_re, target_re + patch_re, 1)
    new_content = new_content.replace(target_parse, target_parse + patch_parse, 1)
    open(path, "w").write(new_content)
    print("Successfully patched send_message_tool.py")
else:
    print("Error: targets not found in send_message_tool.py!")
    sys.exit(1)

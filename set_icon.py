from catalog import save_catalog, assign_icon, payload_icon, load_catalog
import json
import os
import sys
import argparse
import re
import urllib.request
from icon_utils import convert_image_to_data_uri, resolve_icon_source

JSON_FILE = "payloads.json"

def load_payloads():
    if not os.path.exists(JSON_FILE):
        print(f"Error: {JSON_FILE} not found.")
        sys.exit(1)
    return load_catalog()

def save_payloads(payloads):
    from add_payload import reorder_item
    payloads = [reorder_item(p) for p in payloads]
    save_catalog(payloads)

def list_payloads(payloads):
    print("\n" + "=" * 70)
    print(f"{'#':<3} {'Payload Name':<28} {'Version':<15} {'Icon Status'}")
    print("-" * 70)
    for i, p in enumerate(payloads, 1):
        name = p.get("name", "Unknown")
        version = p.get("version", "")
        icon = payload_icon(p)
        if icon:
            fmt = icon.split(";")[0].replace("data:image/", "")
            status = f"✓ {fmt} ({len(icon)} chars)"
        else:
            status = "✗ None"
        print(f"{i:<3} {name:<28} {version:<15} {status}")
    print("=" * 70 + "\n")

def find_payload(payloads, identifier):
    """Finds a payload by 1-based index or case-insensitive name."""
    identifier_str = str(identifier).strip()
    if identifier_str.isdigit():
        idx = int(identifier_str) - 1
        if 0 <= idx < len(payloads):
            return payloads[idx], idx
    for idx, p in enumerate(payloads):
        if p.get("name", "").lower() == identifier_str.lower():
            return p, idx
    return None, -1

def get_suggested_icon(payload):
    """Attempts to find a suggested icon for a payload (README image, repo assets, or author avatar)."""
    source = payload.get("source", "")
    match = re.search(r"https?://([^/]+)/([^/]+)/([^/]+)", source)
    if not match:
        return None
    domain, owner, repo = match.group(1), match.group(2), match.group(3).rstrip("/")
    if domain != "github.com":
        return None

    # Common asset locations to check
    common_asset_paths = [
        "assets/icon0.png",
        "assets/icon.png",
        "icon0.png",
        "icon.png",
        "assets/logo.png",
        "logo.png"
    ]
    for path in common_asset_paths:
        raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/main/{path}"
        try:
            req = urllib.request.Request(raw_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=3) as resp:
                if resp.status == 200:
                    return raw_url
        except Exception:
            pass

    # Fallback to author avatar
    return f"https://github.com/{owner}.png"

def set_payload_icon(payload_identifier, icon_source=None, remove=False):
    payloads = load_payloads()
    payload, idx = find_payload(payloads, payload_identifier)
    
    if not payload:
        print(f"Error: Payload '{payload_identifier}' not found in {JSON_FILE}")
        return False

    name = payload.get("name")

    if remove:
        if "icon_key" in payload:
            del payload["icon_key"]
            save_payloads(payloads)
            print(f"✓ Removed icon from '{name}'.")
        else:
            print(f"Payload '{name}' did not have an icon.")
        return True

    if not icon_source:
        print("Error: No icon source provided.")
        return False

    print(f"Processing icon for '{name}' from: {icon_source}...")
    try:
        data_uri = convert_image_to_data_uri(icon_source)
        assign_icon(payload, data_uri)
        save_payloads(payloads)
        fmt = data_uri.split(";")[0].replace("data:image/", "")
        print(f"✓ Successfully set icon for '{name}' ({fmt}, {len(data_uri)} chars).")
        return True
    except Exception as e:
        print(f"Error setting icon for '{name}': {e}")
        return False

def interactive_mode():
    payloads = load_payloads()
    list_payloads(payloads)

    choice = input("Enter payload number or name to edit (or Enter to exit): ").strip()
    if not choice:
        return

    payload, idx = find_payload(payloads, choice)
    if not payload:
        print(f"Payload '{choice}' not found.")
        return

    name = payload.get("name")
    current_icon = payload_icon(payload)
    print(f"\nSelected: {name}")
    if current_icon:
        print(f"Current icon: {current_icon[:30]}... ({len(current_icon)} chars)")
    else:
        print("Current icon: None")

    suggestion = get_suggested_icon(payload)
    if suggestion:
        print(f"Suggested icon URL: {suggestion}")

    print("\nOptions:")
    print(" - Enter icon URL, file path, or @username")
    if suggestion:
        print(" - Enter 's' to use the suggestion")
    if current_icon:
        print(" - Enter 'r' to remove current icon")
    print(" - Press Enter to cancel")

    action = input("\nYour choice: ").strip()
    if not action:
        print("Cancelled.")
        return
    if action.lower() == "r":
        set_payload_icon(name, remove=True)
    elif action.lower() == "s" and suggestion:
        set_payload_icon(name, icon_source=suggestion)
    else:
        set_payload_icon(name, icon_source=action)

def main():
    parser = argparse.ArgumentParser(description="Manage payload icons in icons.json")
    parser.add_argument("payload", nargs="?", help="Payload name or number")
    parser.add_argument("icon_source", nargs="?", help="Image URL, local path, or @username")
    parser.add_argument("-r", "--remove", action="store_true", help="Remove icon from the payload")
    parser.add_argument("-l", "--list", action="store_true", help="List all payloads and their icon status")

    args = parser.parse_args()

    if args.list:
        payloads = load_payloads()
        list_payloads(payloads)
        return

    if not args.payload:
        interactive_mode()
        return

    if args.remove:
        set_payload_icon(args.payload, remove=True)
    elif args.icon_source:
        set_payload_icon(args.payload, icon_source=args.icon_source)
    else:
        # Prompt for icon
        payloads = load_payloads()
        payload, _ = find_payload(payloads, args.payload)
        if not payload:
            print(f"Error: Payload '{args.payload}' not found.")
            return
        suggestion = get_suggested_icon(payload)
        prompt_text = f"Icon URL, local path, or @owner"
        if suggestion:
            prompt_text += f" (Press Enter to use suggested: {suggestion})"
        prompt_text += ": "
        icon_input = input(prompt_text).strip()
        if not icon_input and suggestion:
            icon_input = suggestion
        if icon_input:
            set_payload_icon(args.payload, icon_source=icon_input)
        else:
            print("Cancelled.")

if __name__ == "__main__":
    main()

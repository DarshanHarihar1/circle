"""
Live MCP tool test — exercises the real Swiggy food tools against your
authenticated session. Reads the access token directly from the DB vault.

Does NOT call place_food_order.

Usage (from circle/backend/):
    python test_mcp_tools.py [host_user_id]

If host_user_id is omitted, the script picks the most-recently updated
HostToken row from the DB.
"""
import asyncio
import json
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

from config import settings  # noqa — loads .env
from db.session import SessionLocal
from db import crud
import vault
from langchain_mcp_adapters.client import MultiServerMCPClient
from agent.nodes.cart_mcp import (
    apply_cart_coupon,
    call_structured,
    parse_cart_pricing,
    resolve_cart_item,
    update_error,
)

FOOD_URL = f"{settings.SWIGGY_MCP_BASE_URL}/food"

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = ""):
    mark = "PASS" if cond else "FAIL"
    (PASS if cond else FAIL).append(name)
    print(f"  [{mark}] {name}" + (f"  ({detail})" if detail else ""))


def pprint(label: str, data):
    text = json.dumps(data, indent=2) if isinstance(data, (dict, list)) else str(data)
    lines = text.splitlines()
    preview = "\n    ".join(lines[:30])
    suffix = f"\n    ... ({len(lines) - 30} more lines)" if len(lines) > 30 else ""
    print(f"\n  >> {label}:\n    {preview}{suffix}")


def extract_text(raw) -> str:
    """Pull the plain-text string out of an MCP tool response.

    langchain-mcp-adapters returns content as a list of
    {type: "text", text: "...", id: "lc_..."} objects.
    This unwraps that into the raw text string.
    """
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        parts = []
        for item in raw:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
            elif isinstance(item, str):
                parts.append(item)
            else:
                parts.append(str(item))
        return "\n".join(parts)
    if isinstance(raw, dict):
        return raw.get("text", json.dumps(raw))
    return str(raw)


def try_parse_json(text: str):
    """Try to parse as JSON; return None on failure."""
    try:
        return json.loads(text)
    except Exception:
        return None


def parse_menu_text(text: str) -> list[dict]:
    """Parse get_restaurant_menu text response.

    Lines look like:
      - Item Name — ₹130 | Veg (ID: 200438182)
      - Item Name — ₹220 | Non-veg, has variants, has addons (ID: 37722681)
    """
    items = []
    for line in text.splitlines():
        m = re.search(r'-\s+(.+?)\s+[—–-]\s+[₹](\d+)\s*\|([^(]+)\(ID:\s*(\d+)\)', line)
        if not m:
            continue
        name, price, flags, item_id = m.group(1).strip(), int(m.group(2)), m.group(3).lower(), m.group(4).strip()
        items.append({
            "id": item_id,
            "name": name,
            "price": price,
            "isVeg": "non-veg" not in flags,
            "hasVariants": "has variants" in flags,
            "hasAddons": "has addons" in flags,
        })
    return items


def _get_token(host_user_id: str | None) -> tuple[str, str]:
    """Return (host_user_id, access_token) from the vault."""
    db = SessionLocal()
    try:
        if host_user_id:
            row = crud.get_host_token(db, host_user_id)
            if not row:
                print(f"No token found for host_user_id={host_user_id}")
                sys.exit(1)
        else:
            from db.models import HostToken
            row = db.query(HostToken).order_by(HostToken.updated_at.desc()).first()
            if not row:
                print("No HostToken rows found. Log in via the app first.")
                sys.exit(1)
            host_user_id = row.host_user_id

        token_data = vault.decrypt(bytes(row.encrypted_token))
        return host_user_id, token_data["access_token"]
    finally:
        db.close()


async def main():
    args = sys.argv[1:]
    query_override = None
    host_uid = None
    for a in args:
        if a.startswith("--query="):
            query_override = a.split("=", 1)[1]
        else:
            host_uid = a
    host_uid, access_token = _get_token(host_uid)
    print(f"\nUsing host_user_id: {host_uid}")
    print(f"Token (first 20 chars): {access_token[:20]}...")
    print(f"MCP endpoint: {FOOD_URL}\n")

    mcp = MultiServerMCPClient({
        "food": {
            "url": FOOD_URL,
            "transport": "streamable_http",
            "headers": {"Authorization": f"Bearer {access_token}"},
        }
    })
    tools = {t.name: t for t in await mcp.get_tools()}

    print(f"Tools available: {sorted(tools.keys())}\n")
    check("place_food_order is present but will NOT be called",
          "place_food_order" in tools)
    check("All expected food tools are present", all(t in tools for t in [
        "get_addresses", "search_restaurants", "get_restaurant_menu",
        "search_menu", "update_food_cart", "get_food_cart",
        "flush_food_cart", "fetch_food_coupons",
    ]))

    # -- 1. get_addresses ------------------------------------------------------
    print("\n-- 1. get_addresses --")
    addr_raw = await tools["get_addresses"].ainvoke({})
    pprint("get_addresses", addr_raw)
    address_id = address_label = None
    addr_text = extract_text(addr_raw)
    # Response is plain text: "1. [Label] Name: street... (ID: abc123)"
    # Try JSON first, fall back to regex
    parsed_json = try_parse_json(addr_text)
    if parsed_json:
        addrs = parsed_json if isinstance(parsed_json, list) else parsed_json.get("addresses", [])
        if addrs:
            first = addrs[0]
            address_id = first.get("id") or first.get("addressId") or first.get("address_id", "")
            address_label = first.get("label") or first.get("name", "")
    if not address_id:
        # Text format: "(ID: abc123)"
        m = re.search(r'\(ID:\s*([^\s)]+)\)', addr_text)
        if m:
            address_id = m.group(1).strip()
        lm = re.search(r'\[([^\]]+)\]', addr_text)
        if lm:
            address_label = lm.group(1).strip()
    check("get_addresses returned data", bool(addr_raw))
    check("Parsed at least one address_id", bool(address_id),
          f"{address_label} → {address_id}" if address_id else "none")

    if not address_id:
        print("\nNo address_id — cannot continue cart tests. Stopping.")
        _summary()
        return

    # -- 2. search_restaurants ------------------------------------------------─
    print("\n-- 2. search_restaurants --")
    query = query_override or "biryani"
    rest_raw = await tools["search_restaurants"].ainvoke({
        "addressId": address_id,
        "query": query,
    })
    pprint(f"search_restaurants (query={query!r})", rest_raw)
    restaurant_id = restaurant_name = None
    rest_text = extract_text(rest_raw)
    parsed_json = try_parse_json(rest_text)
    if parsed_json:
        rlist = parsed_json if isinstance(parsed_json, list) else parsed_json.get("restaurants", parsed_json.get("data", []))
        open_rests = [r for r in rlist
                      if isinstance(r, dict) and
                      (r.get("availabilityStatus") or r.get("availability", "OPEN")).upper() == "OPEN"]
        if open_rests:
            r = open_rests[0]
            restaurant_id = r.get("id") or r.get("restaurantId") or r.get("restaurant_id", "")
            restaurant_name = r.get("name") or r.get("restaurantName") or "Unknown"
    if not restaurant_id:
        # Text format: "1. Restaurant Name — cuisines | rating | ... (ID: 12345)"
        for line in rest_text.splitlines():
            m = re.search(r'\(ID:\s*([^\s)]+)\)', line)
            if m:
                restaurant_id = m.group(1).strip()
                # Name is everything before " — " or " | " after the "N. " prefix
                nm = re.match(r'^\d+\.\s+(.+?)(?:\s+\(Ad\))?\s+[—\|]', line)
                restaurant_name = nm.group(1).strip() if nm else "Unknown"
                break
    check("search_restaurants returned results", bool(rest_raw))
    check("At least one OPEN restaurant found", bool(restaurant_id),
          f"{restaurant_name} ({restaurant_id})" if restaurant_id else "none")

    if not restaurant_id:
        print("\nNo restaurant found — skipping menu + cart tests.")
        _summary()
        return

    print(f"\n  Using restaurant: {restaurant_name} ({restaurant_id})")

    # -- 3. get_restaurant_menu ------------------------------------------------
    print("\n-- 3. get_restaurant_menu --")
    menu_raw = await tools["get_restaurant_menu"].ainvoke({
        "restaurantId": restaurant_id,
        "addressId": address_id,
    })
    pprint("get_restaurant_menu", menu_raw)
    menu_items = []
    menu_text = extract_text(menu_raw)
    parsed_json = try_parse_json(menu_text)
    if parsed_json:
        data = parsed_json
        if isinstance(data, list):
            menu_items = data
        else:
            for key in ("items", "menuItems", "menu", "data", "categories"):
                if key in data and isinstance(data[key], list):
                    for entry in data[key]:
                        if isinstance(entry, dict) and "items" in entry:
                            menu_items.extend(entry["items"])
                        elif isinstance(entry, dict):
                            menu_items.append(entry)
                    if menu_items:
                        break
    if not menu_items:
        menu_items = parse_menu_text(menu_text)
    check("get_restaurant_menu returned items", len(menu_items) > 0,
          f"{len(menu_items)} items")
    if menu_items:
        sample = menu_items[0]
        print(f"  Sample item keys: {list(sample.keys())}")
        check("Menu items have an id/itemId field",
              bool(sample.get("id") or sample.get("itemId")))
        check("Menu items have a name field",
              bool(sample.get("name") or sample.get("itemName")))
        check("Menu items have a price field",
              bool(sample.get("price") or sample.get("finalPrice")))

    # -- 4. search_menu (structured content) -----------------------------------
    print("\n-- 4. search_menu --")
    item_query = query_override or "biryani"
    sm_sc = await call_structured(tools["search_menu"], {
        "query": item_query,
        "restaurantIdOfAddedItem": restaurant_id,
        "addressId": address_id,
    })
    sm_items = sm_sc.get("items") or []
    pprint(f"search_menu (query={item_query!r})",
           {"total": sm_sc.get("total"), "count": len(sm_items)})
    check("search_menu returned items", len(sm_items) > 0, f"{len(sm_items)} items")

    if not sm_items:
        print("\nNo search_menu items — skipping cart tests.")
        _summary()
        return

    sample = sm_items[0]
    print(f"  Sample item keys: {list(sample.keys())}")
    check("search_menu items expose menu_item_id", bool(sample.get("menu_item_id")))
    check("search_menu items expose a name", bool(sample.get("name")))
    n_variant = sum(1 for i in sm_items if i.get("variantsV2") or i.get("variations"))
    print(f"  {n_variant}/{len(sm_items)} items have variants")

    # Resolve a cart-ready item via the SAME helper the backend uses
    planned = {"item_id": sample["menu_item_id"],
               "item_name": sample.get("name", ""), "quantity": 1}
    cart_item = await resolve_cart_item(tools, planned, restaurant_id, address_id)
    print(f"  Resolved cart item: {cart_item}")
    check("resolve_cart_item produced menu_item_id", bool(cart_item.get("menu_item_id")))
    item_has_variants = bool(sample.get("variantsV2") or sample.get("variations"))
    check("resolve_cart_item picked correct variant field",
          (not item_has_variants) or ("variantsV2" in cart_item) or ("variants" in cart_item),
          "variantsV2" if "variantsV2" in cart_item
          else "variants" if "variants" in cart_item else "no variants")

    # -- 5. flush_food_cart (pre-clean) ----------------------------------------
    print("\n-- 5. flush_food_cart (pre-clean) --")
    await tools["flush_food_cart"].ainvoke({"addressId": address_id})
    check("flush_food_cart did not error", True)

    # -- 6. update_food_cart (structured content) ------------------------------
    print("\n-- 6. update_food_cart --")
    update_sc = await call_structured(tools["update_food_cart"], {
        "restaurantId": restaurant_id,
        "addressId": address_id,
        "cartItems": [cart_item],
        "restaurantName": restaurant_name,
    })
    err = update_error(update_sc)
    check("update_food_cart accepted the item (no errorCodes)", err is None, err or "ok")

    # -- 7. get_food_cart (structured content) ---------------------------------
    print("\n-- 7. get_food_cart --")
    cart_sc = await call_structured(tools["get_food_cart"], {
        "addressId": address_id,
        "restaurantName": restaurant_name,
    })
    pricing = await apply_cart_coupon(tools, cart_sc, address_id)
    pprint("cart pricing (after coupon attempt)", pricing)
    check("get_food_cart returned structured data", bool(cart_sc.get("data")))
    check("Cart is not empty after add", not pricing.get("empty"),
          f"item_count={pricing.get('item_count')}")
    check("Cart has a real total (to_pay)", bool(pricing.get("total")),
          f"total={pricing.get('total')}")
    if pricing.get("discount"):
        check("Coupon applied and saving money",
              pricing["discount"] > 0,
              f"{pricing['coupon_code']} saves ₹{pricing['discount']}")

    # -- 8. fetch_food_coupons -------------------------------------------------
    print("\n-- 8. fetch_food_coupons --")
    coup_raw = await tools["fetch_food_coupons"].ainvoke({
        "addressId": address_id,
        "restaurantId": restaurant_id,
    })
    pprint("fetch_food_coupons", coup_raw)
    check("fetch_food_coupons returned data", bool(coup_raw))

    # -- 9. flush_food_cart (cleanup) ------------------------------------------
    print("\n-- 9. flush_food_cart (cleanup) --")
    await tools["flush_food_cart"].ainvoke({"addressId": address_id})
    check("Cart flushed cleanly after test", True)

    _summary()


def _summary():
    print(f"\n{'=' * 60}")
    print(f"RESULT: {len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print(f"FAILED: {', '.join(FAIL)}")
        sys.exit(1)
    print("All checks passed.")


if __name__ == "__main__":
    asyncio.run(main())

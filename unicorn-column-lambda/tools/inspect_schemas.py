"""
Print schema IDs and field names from the tenant so config.FIELDS can be checked.

    python -m tools.inspect_schemas "Packed Column" "Resin Characteristics"
    python -m tools.inspect_schemas --entity bfi_xxxx      # dump one entity's actual field keys/values

Needs BENCHLING_URL / BENCHLING_CLIENT_ID / BENCHLING_CLIENT_SECRET (or BENCHLING_SSM_PARAMETER).
"""
import argparse
import json

from auth_utils.authentication import initialize_benchling


def _print_schema(kind, schema):
    d = schema.to_dict()
    print(f"\n[{kind}] {d.get('name')}  id={d.get('id')}")
    for fd in d.get("fieldDefinitions", []):
        print(f"    name={fd.get('name')!r:45} displayName={fd.get('displayName')!r:40} "
              f"type={fd.get('type')} required={fd.get('isRequired')}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("names", nargs="*", help="substring(s) of schema names to show")
    ap.add_argument("--entity", help="dump the fields of one custom entity")
    args = ap.parse_args()
    b = initialize_benchling()

    if args.entity:
        e = b.custom_entities.get_by_id(args.entity)
        print(json.dumps(e.to_dict().get("fields"), indent=2, default=str))
        return

    wanted = [n.lower() for n in args.names]
    match = lambda s: not wanted or any(w in (s.name or "").lower() for w in wanted)
    for page in b.schemas.list_entity_schemas():
        for s in page:
            if match(s):
                _print_schema("entity", s)
    for page in b.schemas.list_assay_result_schemas():
        for s in page:
            if match(s):
                _print_schema("result", s)


if __name__ == "__main__":
    main()

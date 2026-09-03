#!/usr/bin/env python
"""Build ONE vendor's contact through the real ensure_contacts() path.

    .venv/bin/python work/try_one_contact.py FABI449 [more codes...]

Used to prove the CIN reuse works before committing to a run over 175 vendors,
where every rejection costs six retries with backoff. It writes to the
crosswalk exactly as `masters` does, so a vendor built here is not built twice.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(os.path.join(__file__, "..")))
sys.path.insert(0, HERE)

import post_sage_bills as m                                    # noqa: E402


def main():
    codes = sys.argv[1:]
    if not codes:
        raise SystemExit(__doc__)
    state = m.State()
    api = m.Api()
    held = m.ensure_contacts(api, state, codes)
    print("\n---- result ----")
    for c in codes:
        got = state.xw["contacts"].get(c)
        print("  %-9s %s" % (c, ("BUILT contactId=%s ledger=%s" %
                                 (got["contactId"], got["ledger"])) if got else "not built"))
    for c, why in held:
        print("  HELD %-9s %s" % (c, why))


if __name__ == "__main__":
    main()

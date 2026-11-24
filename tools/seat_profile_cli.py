# -*- coding: utf-8 -*-
import sys
import time

from quantlab.engine import seat_profile as sp

seats = sys.argv[1:] or ["国信证券股份有限公司浙江互联网分公司"]
for s in seats:
    t = time.time()
    p = sp.profile(s)
    print("=" * 70)
    print(sp.render_markdown(p))
    print(f"[{time.time() - t:.1f}s]")

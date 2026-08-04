C:\Users\lane.white>python "C:\Users\lane.white\Downloads\dbg qorvo fetch.py"
dbg_qorvo_fetch: isolating why the Qorvo walk gets HTTP errors
  honest UA : rfparts/2.0 (RF parts sourcing research; contact: set RFPARTS_CONTACT env var)
  ids       : [1141, 1142, 1143, 1108]
  rate      : 1.5s between requests

==========================================================================
  0. is anything between you and the internet?
==========================================================================
    proxy/TLS env vars: none set
    urllib.getproxies(): none
    control: https://example.com           status=200  559B  tables=0  server=cloudflare
      title: Example Domain
    control: https://www.python.org        status=200  11569B  tables=0  server=nginx
    Reading: if the CONTROL hosts fail too, this is your network or
    an outbound proxy, not Qorvo. On a corporate network that is the
    single most likely cause of 'it used to work'.

==========================================================================
  G. did it ever work? (on-disk vendor cache)
==========================================================================
    cache root: C:\Users\lane.white\.rfparts\vendor_cache
    73 cached Qorvo page(s)
      sizes  min=106790 median=140614 max=2239628
      oldest 2026-07-29 15:02   newest 2026-07-29 15:26
      of the first 40, 25 contain a <table>
    Reading: cached pages WITH tables prove fetching used to work and
    the parser had real input. Cached pages with none, or tiny sizes,
    point at responses that were already challenge/blocked pages.

==========================================================================
  H. is the PARSER still fine on a page that used to work?
==========================================================================
    could not import the package: cannot import name 'vendor_catalogs' from 'pythonrfparts' (C:\Users\lane.white\Downloads\pythonrfparts\__init__.py)

==========================================================================
  A. robots.txt -- are we being told not to?
==========================================================================
    fetch robots.txt                       status=200  2019B  tables=0  server=Vercel
    can_fetch('rfparts/2.0 (RF parts sourci') -> True
    can_fetch('rfparts'                     ) -> True
    can_fetch('*'                           ) -> True
    robots.txt has 37 relevant line(s); showing any that mention 'product':
      User-agent: *
      Disallow: /products/c/
      Disallow: /products/i/
      Disallow: /products/s/
      Disallow: /products/sc/
      Disallow: /products/sca/
      Disallow: /products/ai/
      User-agent: GPTBot
      User-agent: ChatGPT-User
      User-agent: Claude-Web
      User-agent: anthropic-ai
      User-agent: CCBot
      User-agent: Google-Extended
      User-agent: FacebookBot
      User-agent: Omgilibot
      User-agent: Diffbot
      User-agent: Bytespider
      User-agent: cohere-ai
      User-agent: PerplexityBot

==========================================================================
  B-E. same URL, different request shapes (categoryID=1141)
==========================================================================
    B current fetcher headers              status=404  HTTPError 404 Not Found  4000B  server=Vercel  !! WAF/challenge hint: 'bot'
    C honest UA + normal headers           status=404  HTTPError 404 Not Found  4000B  server=Vercel  !! WAF/challenge hint: 'bot'
    D honest UA + browser headers          status=404  HTTPError 404 Not Found  4000B  server=Vercel  !! WAF/challenge hint: 'bot'
    E browser UA                          SKIPPED (pass --allow-browser-ua; see the note at the top)

==========================================================================
  F. is the URL scheme still right? (categoryID=1141)
==========================================================================
    current (what the walk uses)           status=404  HTTPError 404 Not Found  4000B  server=Vercel  !! WAF/challenge hint: 'bot'
    trailing slash on path                 status=404  HTTPError 404 Not Found  4000B  server=Vercel  !! WAF/challenge hint: 'bot'
    no www                                 status=404  HTTPError 404 Not Found  4000B  server=Vercel  !! WAF/challenge hint: 'bot'
    newer /products/search form            status=404  HTTPError 404 Not Found  4000B  server=Vercel  !! WAF/challenge hint: 'bot'
    plain /products page                   status=200  1127334B  tables=0  server=Vercel
      title: Products - Qorvo

==========================================================================
  I. is it every id, or only some?
==========================================================================
    categoryID=1142                        status=404  HTTPError 404 Not Found  4000B  server=Vercel  !! WAF/challenge hint: 'bot'
    categoryID=1143                        status=404  HTTPError 404 Not Found  4000B  server=Vercel  !! WAF/challenge hint: 'bot'
    categoryID=1108                        status=404  HTTPError 404 Not Found  4000B  server=Vercel  !! WAF/challenge hint: 'bot'

==========================================================================
  VERDICT
==========================================================================
    current fetcher shape (B): status=404
    -> 404: the URL scheme is gone. Check section F for a form
       that returns 200 and repoint the walk at it.
    URL forms that returned 200: ['plain /products page']

  Paste this whole output back and I will make the matching fix.

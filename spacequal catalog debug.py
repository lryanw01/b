============================================================================
CATALOG PAGES: fetch -> render check -> parse
============================================================================

  Qorvo  [qorvo]
    researched: server-rendered tables, PN + datasheet id per row
    [ok     ]   3.0s  HTTP 200  897 kB  514 links, 5707 words
               https://www.qorvo.com/products/product-list?categoryID=ca0118
               parsed 106 part(s), 66 with a datasheet link
                 QM42500                  (none)
                 QM42639                  (none)
                 QM42655                  (none)
                 QPF4200                  https://www.qorvo.com/products/d/da006470
                 QPF4202                  (none)
                 QPF4206                  (none)
                 QPF4206B                 (none)
                 QPF4207                  https://www.qorvo.com/products/d/da009265
    [ok     ]   0.7s  HTTP 200  125 kB  309 links, 1158 words
               https://www.qorvo.com/products/active-antenna-systems/beamformers
               parsed 0 part(s), 0 with a datasheet link
    [ok     ]   0.7s  HTTP 200  117 kB  305 links, 1082 words
               https://www.qorvo.com/products/active-antenna-systems/front-end-modules
               parsed 0 part(s), 0 with a datasheet link
    [ok     ]   0.5s  HTTP 200  109 kB  305 links, 1088 words
               https://www.qorvo.com/products/active-antenna-systems/if-transceivers
               parsed 0 part(s), 0 with a datasheet link
    [ok     ]   0.9s  HTTP 200  134 kB  313 links, 1154 words
               https://www.qorvo.com/products/amplifiers/distributed-amplifiers
               parsed 0 part(s), 0 with a datasheet link

  Marki Microwave  [marki]
    researched: category pages list products; per-product datasheet page
    [ok     ]   1.0s  HTTP 200  308 kB  239 links, 2258 words
               https://markimicrowave.com/products/connectorized/mixers/
               parsed 97 part(s), 97 with a datasheet link
                 MM1-0320LBH              https://markimicrowave.com/products/connectorized/mixers/mm1-0320lbh/d
                 MM1-1886HM               https://markimicrowave.com/products/connectorized/mixers/mm1-1886hm/da
                 MM2-0530LBH              https://markimicrowave.com/products/connectorized/mixers/mm2-0530lbh/d
                 MM1-1886LM               https://markimicrowave.com/products/connectorized/mixers/mm1-1886lm/da
                 M1-0420MA                https://markimicrowave.com/products/connectorized/mixers/m1-0420ma/dat
                 M2-0208LP                https://markimicrowave.com/products/connectorized/mixers/m2-0208lp/dat
                 M2-0240LP                https://markimicrowave.com/products/connectorized/mixers/m2-0240lp/dat
                 M2-0240LPV               https://markimicrowave.com/products/connectorized/mixers/m2-0240lpv/da
    [ok     ]   1.3s  HTTP 200  300 kB  439 links, 1836 words
               https://markimicrowave.com/products/surface-mount/mixers/
               parsed 76 part(s), 76 with a datasheet link
                 MM1-1850HPSM-2           https://markimicrowave.com/products/surface-mount/mixers/mm1-1850hpsm-
                 ADM-8007PSM              https://markimicrowave.com/products/surface-mount/amplifiers/adm-8007p
                 MM2-0843HPSM-1           https://markimicrowave.com/products/surface-mount/mixers/mm2-0843hpsm-
                 ADM1-8007APC             https://markimicrowave.com/products/connectorized/amplifiers/adm1-8007
                 MM2-0843HPSM-2           https://markimicrowave.com/products/surface-mount/mixers/mm2-0843hpsm-
                 MM2-0432HPSM-2           https://markimicrowave.com/products/surface-mount/mixers/mm2-0432hpsm-
                 MM2-0432HPSM-1           https://markimicrowave.com/products/surface-mount/mixers/mm2-0432hpsm-
                 MM1A-0330HPSM            https://markimicrowave.com/products/surface-mount/mixers/mm1a-0330hpsm
    [ok     ]   0.9s  HTTP 200  178 kB  160 links, 1250 words
               https://markimicrowave.com/products/connectorized/amplifiers/
               parsed 32 part(s), 32 with a datasheet link
                 AMM2-0020UH              https://markimicrowave.com/products/connectorized/amplifiers/amm2-0020
                 AMM2-0070UH              https://markimicrowave.com/products/connectorized/amplifiers/amm2-0070
                 ADM1-8007APC             https://markimicrowave.com/products/connectorized/amplifiers/adm1-8007
                 AMM-9893M                https://markimicrowave.com/products/connectorized/amplifiers/amm-9893m
                 AMM-7200UC-K             https://markimicrowave.com/products/connectorized/amplifiers/amm-7200u
                 ADM-8622PC               https://markimicrowave.com/products/connectorized/amplifiers/adm-8622p
                 ADM-8625PC               https://markimicrowave.com/products/connectorized/amplifiers/adm-8625p
                 ADM-8344PC               https://markimicrowave.com/products/connectorized/amplifiers/adm-8344p

  Analog Devices  [adi]
    datasheet pattern known; LISTING page unproven -- probing candidates
    [error  ]   0.6s  https://www.analog.com/en/product-category/rf-microwave.html
               -> HTTPError: HTTP Error 403: Forbidden
    [ok     ]   0.7s  HTTP 200  23 kB  45 links, 76 words
               https://www.analog.com/en/products.html
               parsed 0 part(s), 0 with a datasheet link

  MACOM  [macom]
    datasheet pattern known; LISTING page unproven -- probing candidates
    [ok     ]   0.7s  HTTP 200  58 kB  222 links, 706 words
               https://www.macom.com/products
               parsed 0 part(s), 0 with a datasheet link
    [error  ]   0.3s  https://www.macom.com/products/rf-microwave
               -> HTTPError: HTTP Error 404: Not Found

  Skyworks  [skyworks]
    datasheet pattern known; LISTING page unproven -- probing candidates
    [ok     ]   0.8s  HTTP 200  199 kB  1198 links, 2699 words
               https://www.skyworksinc.com/en/Products
               parsed 0 part(s), 0 with a datasheet link
    [ok     ]   0.7s  HTTP 200  218 kB  1216 links, 2949 words
               https://www.skyworksinc.com/en/Products/Amplifiers
               parsed 0 part(s), 0 with a datasheet link

============================================================================
DISCOVERY: find listing URLs from a seed page, then follow up to 4
============================================================================
  This is the capability the first run showed was missing: MACOM and
  Skyworks seed pages list only categories, and Qorvo's tables live
  only behind product-list?categoryID=<id>.

  Qorvo              discovered 1 candidate listing URL(s)
      https://www.qorvo.com/products/product-list?categoryID=ca0118
    [ok     ]   3.3s  897 kB  514 links  5707 words  -> 106 part(s), 66 with datasheet
                 QM42500                  (none)
                 QM42639                  (none)
                 QM42655                  (none)
                 QPF4200                  https://www.qorvo.com/products/d/da006470
                 QPF4202                  (none)
                 QPF4206                  (none)
  Marki Microwave    discovered 4 candidate listing URL(s)
      https://markimicrowave.com/products/connectorized/mixers/double-balanced-mixers/
      https://markimicrowave.com/products/connectorized/mixers/t3-high-linearity/
      https://markimicrowave.com/products/connectorized/mixers/triple-balanced/
      https://markimicrowave.com/products/connectorized/mixers/integrated-drive-mixers/
    [ok     ]   0.9s  310 kB  235 links  2142 words  -> 97 part(s), 97 with datasheet
                 T3-07MQP                 https://markimicrowave.com/products/connectorized/mixers/t3-07mqp/
                 T3-07LQP                 https://markimicrowave.com/products/connectorized/mixers/t3-07lqp/
                 T3A-07PA                 https://markimicrowave.com/products/connectorized/mixers/t3a-07pa/
                 T3-12MQP                 https://markimicrowave.com/products/connectorized/mixers/t3-12mqp/
                 T3-12LQP                 https://markimicrowave.com/products/connectorized/mixers/t3-12lqp/
                 MM1-0212SS               https://markimicrowave.com/products/connectorized/mixers/mm1-0212s
    [ok     ]   1.1s  310 kB  235 links  2154 words  -> 97 part(s), 97 with datasheet
                 T3-07MQP                 https://markimicrowave.com/products/connectorized/mixers/t3-07mqp/
                 T3-07LQP                 https://markimicrowave.com/products/connectorized/mixers/t3-07lqp/
                 T3A-07PA                 https://markimicrowave.com/products/connectorized/mixers/t3a-07pa/
                 T3-12MQP                 https://markimicrowave.com/products/connectorized/mixers/t3-12mqp/
                 T3-12LQP                 https://markimicrowave.com/products/connectorized/mixers/t3-12lqp/
                 MM1-0212SS               https://markimicrowave.com/products/connectorized/mixers/mm1-0212s
    [ok     ]   2.1s  309 kB  235 links  2103 words  -> 97 part(s), 97 with datasheet
                 T3-07MQP                 https://markimicrowave.com/products/connectorized/mixers/t3-07mqp/
                 T3-07LQP                 https://markimicrowave.com/products/connectorized/mixers/t3-07lqp/
                 T3A-07PA                 https://markimicrowave.com/products/connectorized/mixers/t3a-07pa/
                 T3-12MQP                 https://markimicrowave.com/products/connectorized/mixers/t3-12mqp/
                 T3-12LQP                 https://markimicrowave.com/products/connectorized/mixers/t3-12lqp/
                 MM1-0212SS               https://markimicrowave.com/products/connectorized/mixers/mm1-0212s
    [ok     ]   1.0s  310 kB  235 links  2129 words  -> 97 part(s), 97 with datasheet
                 T3-07MQP                 https://markimicrowave.com/products/connectorized/mixers/t3-07mqp/
                 T3-07LQP                 https://markimicrowave.com/products/connectorized/mixers/t3-07lqp/
                 T3A-07PA                 https://markimicrowave.com/products/connectorized/mixers/t3a-07pa/
                 T3-12MQP                 https://markimicrowave.com/products/connectorized/mixers/t3-12mqp/
                 T3-12LQP                 https://markimicrowave.com/products/connectorized/mixers/t3-12lqp/
                 MM1-0212SS               https://markimicrowave.com/products/connectorized/mixers/mm1-0212s
  Analog Devices     seed fetch failed: HTTPError: HTTP Error 403: Forbidden
  MACOM              discovered 4 candidate listing URL(s)
      https://www.macom.com/products/rf-microwave-mmwave/amplifiers/catv/active-splitters
      https://www.macom.com/products/rf-microwave-mmwave/amplifiers/catv/catv-amplifiers
      https://www.macom.com/products/rf-microwave-mmwave/amplifiers/catv/fttx-amplifiers
      https://www.macom.com/products/rf-microwave-mmwave/amplifiers/hybrid-amplifiers/hybrid-amplifiers-gain-block
    [ok     ]   1.5s  191 kB  237 links  858 words  -> 0 part(s), 0 with datasheet
    [ok     ]   1.7s  271 kB  251 links  983 words  -> 0 part(s), 0 with datasheet
    [ok     ]   0.3s  98 kB  226 links  815 words  -> 0 part(s), 0 with datasheet
    [ok     ]   5.7s  789 kB  356 links  1223 words  -> 0 part(s), 0 with datasheet
  Skyworks           discovered 4 candidate listing URL(s)
      https://www.skyworksinc.com/en/Products/Timing/NetSync-Network-Synchronizer-Clocks
      https://www.skyworksinc.com/en/Products/TV-and-Video/Evaluation-Kits#collapseall
      https://www.skyworksinc.com/en/Products/Voice/Si3000-Voice-Codecs
      https://www.skyworksinc.com/en/Products/Voice/Voice-DAAs
    [ok     ]   0.6s  199 kB  1174 links  3003 words  -> 0 part(s), 0 with datasheet
    [ok     ]   0.6s  214 kB  1205 links  3076 words  -> 0 part(s), 0 with datasheet
    [ok     ]   0.9s  194 kB  1184 links  2584 words  -> 0 part(s), 0 with datasheet
    [ok     ]   1.0s  196 kB  1174 links  2757 words  -> 0 part(s), 0 with datasheet

============================================================================
DATASHEET PDFs: fetch -> %PDF magic -> text extraction
============================================================================

  qorvo: trying 2 of 132 datasheet URL(s)
    [no-pdf ]   0.3s  QPF4200              HTML with no reachable PDF link (0 candidate(s)); page says: "function OptanonWrapper() { } Important Notice - Qorvo window.isIE8 = true; (function(i,s,o,g,r,a,m){i['GoogleAnalyticsO"
    [ok     ]   0.5s  QPF4207              174 kB  %PDF  (HTTP 200 direct)
               extracted 5367 chars / 848 words
               "Classification | PRIVATE QPF4207 Wi-Fi 7 Front End Module Data Sheet Brief Rev A, May 2024 1 of 2 www.qorvo.com Subject to change without notice | All"
               construction cues present: NONE

  marki: trying 2 of 593 datasheet URL(s)
    [ok     ]   2.1s  MM1-0320LBH          2929 kB  %PDF  (followed HTML -> MM_Catalog_Connectorized_Waveguide_7-2026.pdf)
               text extraction FAILED: __EXTRACT_FAILED__ PdfminerException
    [ok     ]   3.2s  MM1-1886HM           2929 kB  %PDF  (followed HTML -> MM_Catalog_Connectorized_Waveguide_7-2026.pdf)
               text extraction FAILED: __EXTRACT_FAILED__ PdfminerException

============================================================================
CONTROL: Mini-Circuits (local JSON catalog -> PDF)
============================================================================
  loaded 15939 parts from minicircuits_products_full.json
    [ok     ]   4.7s  10F-10F+           261 kB  %PDF  278 words extracted
    [ok     ]   0.4s  10F-10M+           295 kB  %PDF  287 words extracted

============================================================================
SCORECARD
============================================================================
  vendor               parts  with ds   verdict
  Qorvo                  212      132   READY to wire in
  Marki Microwave        593      593   READY to wire in
  Analog Devices           0        0   no parts parsed -- save pages and use --local
  MACOM                    0        0   no parts parsed -- save pages and use --local
  Skyworks                 0        0   no parts parsed -- save pages and use --local

WHAT TO CONCLUDE
============================================================================
  For each vendor, three things had to work:
    fetch     the listing page came back at all
    parse     part numbers AND datasheet links came out of it
    pdf       one of those links returned a real %PDF with readable text
  A vendor is only worth wiring in when all three pass. If a page came
  back but parsed 0 parts and says 'looks JS-rendered', its catalog is
  built in the browser and the answer is to save pages from your own
  Chrome and re-run with --local.

report written to C:\Users\lane.white\spacequal_catalog_report.txt
PS C:\Users\lane.white>




    Also of note, ADI has a full excel of qualified parts to use in the main training program at: "C:\Users\lane.white\Downloads\newSources\adi_space_portfolio_2026-07-28.xlsx"

Fix ADI, MACOM, and Skyworks now.

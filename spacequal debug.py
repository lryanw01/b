
==========================================================================
1. ENVIRONMENT
==========================================================================
  python           3.12.8  (C:\EngTools\Python3128\python.exe)
  platform         Windows-11-10.0.26200-SP0
  ssl              OpenSSL 3.0.15 3 Sep 2024
  default timeout  15.0  (set by this script)
  cwd              C:\Users\lane.white
  SPACEQUAL_HOME   C:\Users\lane.white\.spacequal  (exists=True)

  proxy / network environment variables:
    (none set)
    NOTE: if your browser reaches these sites through a corporate
    proxy configured by PAC/WPAD, python will NOT pick that up. That
    alone can cause exactly the hang you saw.
  urllib.getproxies() -> {}
  certifi          C:\EngTools\Python3128\Lib\site-packages\certifi\cacert.pem
  pdfplumber       0.11.10
  pypdf            MISSING
  sklearn          1.6.1
  numpy            2.1.3
  joblib           1.4.2

==========================================================================
2. DNS RESOLUTION  (budget 8s each)
==========================================================================
  [ok     ]   0.18s www.minicircuits.com       18.217.58.244
  [ok     ]   0.11s www.analog.com             23.204.199.15
  [ok     ]   0.05s cdn.macom.com              104.18.20.123
  [ok     ]   0.09s www.skyworksinc.com        52.9.185.146
  [ok     ]   0.03s markimicrowave.com         172.66.41.36
  [ok     ]   0.08s www.qorvo.com              13.248.213.164

==========================================================================
3. TCP CONNECT to :443   (budget 10s each)
==========================================================================
  [ok     ]   0.06s www.minicircuits.com       connected to 18.217.58.244:443
  [ok     ]   0.05s www.analog.com             connected to 23.204.199.15:443
  [ok     ]   0.05s cdn.macom.com              connected to 104.18.20.123:443
  [ok     ]   0.10s www.skyworksinc.com        connected to 52.9.185.146:443
  [ok     ]   0.05s markimicrowave.com         connected to 172.66.41.36:443
  [ok     ]   0.06s www.qorvo.com              connected to 13.248.213.164:443

==========================================================================
4. TLS HANDSHAKE   (budget 12s each)
==========================================================================
  [ok     ]   0.14s www.minicircuits.com       TLSv1.2  issuer=BAE Systems, Inc.
           ^ unusual issuer 'BAE Systems, Inc.': looks like TLS inspection by a corporate middlebox
  [ok     ]   0.16s www.analog.com             TLSv1.3  issuer=DigiCert Inc
  [ok     ]   3.05s cdn.macom.com              TLSv1.3  issuer=BAE Systems, Inc.
           ^ unusual issuer 'BAE Systems, Inc.': looks like TLS inspection by a corporate middlebox
  [ok     ]   0.26s www.skyworksinc.com        TLSv1.3  issuer=BAE Systems, Inc.
           ^ unusual issuer 'BAE Systems, Inc.': looks like TLS inspection by a corporate middlebox
  [ok     ]   0.12s markimicrowave.com         TLSv1.3  issuer=BAE Systems, Inc.
           ^ unusual issuer 'BAE Systems, Inc.': looks like TLS inspection by a corporate middlebox
  [ok     ]   0.16s www.qorvo.com              TLSv1.3  issuer=BAE Systems, Inc.
           ^ unusual issuer 'BAE Systems, Inc.': looks like TLS inspection by a corporate middlebox

==========================================================================
5. ROBOTS.TXT   <-- prime suspect for the hang
==========================================================================
  [ok     ]   0.20s GET  https://www.minicircuits.com/robots.txt
            -> HTTP 200, 1019 bytes
  [ok     ]   0.17s RobotFileParser -> allow_all=False disallow_all=False can_fetch(sample)=True

  [error  ]  10.11s GET  https://www.analog.com/robots.txt
            -> TimeoutError: The read operation timed out

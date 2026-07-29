==============================================================
  STEP 3/4: TRAIN
==============================================================
training data: 871 positives, 106 unlabeled (14900 parts skipped for missing datasheet text)
  ! only 106 unlabeled part(s) for 871 positives. PU bagging draws its negatives from U, so a pool smaller than the
    positive set makes every bag nearly identical and the estimates unstable.
    Fetch more parts (the queue interleaves P and U, so just run fetch again).
  featurizing with tfidf ...
  feature matrix: 977 x 57491
  PU bagging: 15 bags x 5 folds
    fold 1/5: held-out P mean score 0.961
    fold 2/5: held-out P mean score 0.958
    fold 3/5: held-out P mean score 0.962
    fold 4/5: held-out P mean score 0.958
    fold 5/5: held-out P mean score 0.962
  domain priors blended in at weight 1.0 (17 terms)

  decision threshold 0.9278 (chosen for 90% recall on held-out P)

  top features pushing TOWARD qualifiable:
    +0.291  word__mhz
    +0.265  word__attenuation
    +0.219  word__with
    +0.212  word__power
    +0.212  word__attenuator
    +0.210  word__fixed
    +0.201  word__handling
    +0.197  word__power handling
    +0.194  word__terms
    +0.193  char__tte
    +0.190  word__high
    +0.187  word__flatness
    +0.186  word__bw
    +0.179  char__tten
    +0.179  char__atten
    +0.179  char__ atte
    +0.178  char__enu
    +0.178  char__ttenu
    +0.178  char__tenu
    +0.178  char__tenua
    +0.178  char__nuat
    +0.178  char__enua
    +0.178  char__enuat
    +0.177  char__uat
    +0.176  word__feature
  top features pushing AWAY:
    -0.964  word__adapter
    -0.770  word__coaxial adapter
    -0.676  word__connector
    -0.549  word__mm male
    -0.548  char__dap
    -0.548  char__dapt
    -0.548  char__apt
    -0.547  char__adap
    -0.547  char__ adap
    -0.547  char__ ada

  saved model -> C:\Users\lane.white\.spacequal\model.joblib
  saved predictions -> C:\Users\lane.white\.spacequal\predictions.json

Positive-Unlabeled evaluation
==========================================================
  known space parts (P)        871
  unlabeled catalog parts (U)  106

  RECALL ON P                  90.0%   (784/871)   [held out]
    missed known space parts   87   <- real errors

  flag rate on U               0.0%   (0/106)
    NOT an error rate: U is unlabeled, so a flag here is a
    candidate to review, and some are genuinely qualifiable.

  PU score (recall^2/P(flag))   1.010   <- compare backends with this

  sanity negatives (eval boards, kits, adapters): 106
    flagged                    0 (0.0%)   <- want LOW; heuristic set, not ground truth

  Precision on U cannot be measured from PU data. Scenarios:
    assumed prevalence | qualifiable in U | est. precision | est. finds
                  1% |                1 |           0.0% |          0
                  2% |                2 |           0.0% |          0
                  3% |                3 |           0.0% |          0
                  5% |                5 |           0.0% |          0
                 10% |               11 |           0.0% |          0
                 20% |               21 |           0.0% |          0

  Next: `review` to export the ranked candidate queue, label a
  sample, then `eval --reviewed labelled.csv` for real precision.

==============================================================
  STEP 4/4: REVIEW
==============================================================
wrote 0 candidates -> review_queue.csv
Fill the 'verdict' column with y/n, then:
  python spacequal.py eval --reviewed review_queue.csv
also wrote 87 MISSED known-space parts -> review_queue_missed_positives.csv
  (real errors; read these to improve features or text extraction)

==============================================================
  Done. Model: C:\Users\lane.white\.spacequal\model.joblib
  Coverage grows each time: re-run option 1 to fetch the next 800 part(s).
  Cached datasheets are reused, so nothing is downloaded twice.
  Score a paragraph:  python spacequal.py predict --text "..."
  Or menu option 7.

  press Enter to continue

==============================================================
  spacequal - is this RF part space-qualifiable?
==============================================================
  dataset : 886 P / 14991 U
  cache   : 977 datasheet text file(s)
  model   : tfidf, thr 93%, 2026-07-29 11:19
  catalog : C:\Users\lane.white\Downloads\rfparts\rfparts\sources\minicircuits_products_full.json
--------------------------------------------------------------
  1) Run everything   (match -> fetch -> train -> review)
  2) Match catalog to everythingRF space parts
  3) Fetch datasheets            (verbose progress)
  4) Train classifier
  5) Evaluate (PU metrics)
  6) Export review queue
  7) Score a paragraph of text   <- the everyday tool
  8) Settings
  9) Offline selftest (no network or catalog needed)
  c) Clear cache / reset
  0) Quit
--------------------------------------------------------------
  choice> 7

  model: tfidf  trained 2026-07-29 11:19  on 871 positives
  Paste a datasheet paragraph. Blank line scores it; 'q' returns.

  > •High Frequency: 8-9 GHz
•Low Noise Figure : +3.5 dB Typical
•Laser Welded Housing for Ultimate Environmental Protection
•Leadfree Option: Model BXHF1275LF


  (pasted text)
    chance space-qualifiable    90.2%     -> not flagged
    (threshold 92.8% chosen at training time)
    learned model alone         90.2%
    domain priors: no terms matched

  > ModelBXHF1275isahighfrequencyamplifiercovering8-9GHz.Thisdesignutilizesalasersealed
housing for superior environmental protection. This standard designmay also be ordered in a
screened MIL-STD-883 version (Model #SXHF1275.) All specification ratings are based on
measurementsina50Ω(ohm)systemwithaDCsupplyvoltagetoleranceof+/-2%.


  (pasted text)
    chance space-qualifiable    93.1%     -> LIKELY QUALIFIABLE
    (threshold 92.8% chosen at training time)
    learned model alone         93.1%
    domain priors: no terms matched

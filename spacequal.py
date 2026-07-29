==============================================================
  STEP 3/4: TRAIN
==============================================================
training data: 871 positives, 106 unlabeled (14900 parts skipped for missing datasheet text)
  featurizing with tfidf ...
  feature matrix: 977 x 57491
  PU bagging: 15 bags x 5 folds
Traceback (most recent call last):
  File "C:\Users\lane.white\Downloads\spacequal.py", line 1766, in <module>
    sys.exit(main())
             ^^^^^^
  File "C:\Users\lane.white\Downloads\spacequal.py", line 1750, in main
    return menu()
           ^^^^^^
  File "C:\Users\lane.white\Downloads\spacequal.py", line 1613, in menu
    cmd_runall(_ns(catalog=s["catalog"]))
  File "C:\Users\lane.white\Downloads\spacequal.py", line 1477, in cmd_runall
    rc = fn()
         ^^^^
  File "C:\Users\lane.white\Downloads\spacequal.py", line 1472, in <lambda>
    ("TRAIN", lambda: cmd_train(_train_ns(s))),
                      ^^^^^^^^^^^^^^^^^^^^^^^
  File "C:\Users\lane.white\Downloads\spacequal.py", line 934, in cmd_train
    p_scores, u_scores, models = train_pu(Xp, Xu, n_bags=args.bags,
                                 ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "C:\Users\lane.white\Downloads\spacequal.py", line 566, in train_pu
    u_sum[oob] += clf.predict_proba(Xu[oob])[:, 1]
                  ^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "C:\EngTools\Python3128\Lib\site-packages\sklearn\linear_model\_logistic.py", line 1428, in predict_proba
    return super()._predict_proba_lr(X)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "C:\EngTools\Python3128\Lib\site-packages\sklearn\linear_model\_base.py", line 389, in _predict_proba_lr
    prob = self.decision_function(X)
           ^^^^^^^^^^^^^^^^^^^^^^^^^
  File "C:\EngTools\Python3128\Lib\site-packages\sklearn\linear_model\_base.py", line 351, in decision_function
    X = validate_data(self, X, accept_sparse="csr", reset=False)
        ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "C:\EngTools\Python3128\Lib\site-packages\sklearn\utils\validation.py", line 2944, in validate_data
    out = check_array(X, input_name="X", **check_params)
          ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "C:\EngTools\Python3128\Lib\site-packages\sklearn\utils\validation.py", line 1130, in check_array
    raise ValueError(
ValueError: Found array with 0 sample(s) (shape=(0, 57491)) while a minimum of 1 is required by LogisticRegression.
PS C:\Users\lane.white>

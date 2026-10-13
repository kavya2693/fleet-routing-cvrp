"""Next-day routing for a regional freight fleet."""

import os

# Hundreds of tiny statsmodels fits are much slower with a multi-threaded BLAS than with one
# thread (thread start-up dominates), and this machine is shared. Set before numpy loads.
for _var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "VECLIB_MAXIMUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_var, "1")

# evalh-plugin-pkg

Tiny external package used by `tests/integration/test_plugin_loading.py` to
verify the eval-harness entry-point plugin mechanism end-to-end.

Registers `SqlEquivalentEvaluator` under `eval_harness.evaluators` as
`sql_equivalent`. No eval-harness source is modified — the plugin lands in the
registry purely through `importlib.metadata.entry_points`.

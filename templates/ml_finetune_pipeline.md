Domain: PyTorch/HuggingFace fine-tuning pipeline.
Criteria hints: checkpoint save/load so an interrupted run resumes without
losing progress, a clear train/eval split, and a validation step that
would catch the model silently misbehaving (not just "loss goes down").
Free-tier compute assumption: don't assume unlimited GPU time is available.

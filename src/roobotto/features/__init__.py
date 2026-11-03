"""Causal feature extractors (T-10). Each one is a streaming object: push one block of past
audio, get one value back. Every analysis window ends at "now" and never straddles it, nothing
is normalized over the whole recording, and nothing here decides anything (AGENTS.md rule 3)."""

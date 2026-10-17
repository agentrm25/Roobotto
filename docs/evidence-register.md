# Evidence register

Every research or hardware claim Roobotto relies on gets an entry here: claim, source, status,
local test, result (AGENTS.md rule 11). Anything unconfirmed is marked **[verify]**.

Status values:

- **Documented**: stated in a vendor manual, README, or package metadata, but not yet tested on our rig.
- **Verified**: tested on our rig, with a bench log in `captures/`.
- **Assumed**: must be tested before anything depends on it.

Seeded from `implementation-plan.md` Appendix C. The status is Documented until a bench log exists. These sources support only the specific statements listed; they verify nothing about our wiring.

| ID | Claim | Source | Status | Local test | Result |
|---|---|---|---|---|---|
| E-01 | Helix Stadium receives CC69 0–7 for snapshots 1–8, 8 for next, 9 for previous. It sends CC69 on snapshot change when *Send Snapshot CC* is on, and sends a Program Change on hardware preset change. MIDI goes over 5-pin and USB-C, with no driver on macOS. Bypass/Control is on channel 2; the Matrix Mixer is on channels 3, 4, and 5. Song transport uses CC63, CC10, and CC46–51. | [Helix Stadium manual — MIDI](https://manuals.line6.com/en/helix-stadium/live/midi) | Documented | H-1, H-2, H-5 | Not yet run |
| E-02 | Command Center: up to 16 Instant commands per preset, with values that can change per snapshot, sent to 5-pin and USB by default | [Helix Stadium manual — Command Center](https://manuals.line6.com/en/helix-stadium/live/command-center) | Documented | H-4 | Not yet run |
| E-03 | Showcase is the built-in automation and playback engine: songs with up to 8 tracks and flags, automating preset and snapshot recall and MIDI | [Helix Stadium manual — Song View](https://manuals.line6.com/en/helix-stadium/live/song-view) | Documented | H-2 | Not yet run |
| E-04 | USB audio on macOS: Core Audio, 48 kHz/24-bit. USB 7 and 8 are dry DI sends with selectable sources; USB In 3/4 can be the re-amp source. 8 in/out (stated for Windows ASIO; a third-party report says the same for macOS). | [Helix Stadium manual — USB Audio](https://manuals.line6.com/en/helix-stadium/live/usb-audio); [third-party walkthrough](https://ilikekillnerds.com/2026/06/15/helix-stadium-xl-recording/) | Documented | H-6 | Not yet run |
| E-05 | Helix Stadium XL I/O: two instrument inputs, one XLR mic input, Returns 1–4, 5-pin MIDI In and Out/Thru, USB-C to a computer | [Helix Stadium manual — Rear Panel](https://manuals.line6.com/en/helix-stadium/live/rear-panel) | Documented | H-6 | Not yet run |
| E-06 | H4MIDI WC: 2×2 DIN; USB-A host (up to 8 devices through a hub); USB-C to a computer with 4×4 virtual ports; routing, merging, splitting, filtering, and mapping; 4 presets without a computer; Bluetooth via the optional WIDI Core | [CME product page](https://www.cme-pro.com/h4midi-wc-usb-host-midi-interface/) (vendor claims) | Documented | R-1, R-2 | Not yet run |
| E-07 | 4Cast MVP plays video and DMX lighting cues recorded as Art-Net, triggered by MIDI notes | [Sonicstate, NAMM 2023](https://sonicstate.com/news/2023/04/14/autoyt-namm-2023-namm23a14castmov) (secondary) | Documented | M-1, M-2 | Not yet run |
| E-08 | Matchmaker: Apache-2.0. Implements OLTW (Dixon), OLTW (Arzt), and an HMM. Audio-to-score only, evaluated on solo piano. Version 0.3.0 requires Python older than 3.13 and numpy older than 2. | [GitHub](https://github.com/pymatchmaker/matchmaker); [arXiv 2510.10087](https://arxiv.org/abs/2510.10087); [PyPI](https://pypi.org/project/pymatchmaker/) | Documented | none; reference implementation only | Not yet run |
| E-09 | Beat This!: MIT for code and weights; processes whole files (no streaming mode described) | [GitHub](https://github.com/CPJKU/beat_this) | Documented | Offline grids only | Not yet run |
| E-10 | madmom: latest PyPI release is 0.16.1; code is BSD and models are CC BY-NC-SA 4.0; the PyPI release only supports Python older than 3.10 and numpy older than 1.20, so install from git | [PyPI](https://pypi.org/project/madmom/); [GitHub](https://github.com/CPJKU/madmom); Beat This! README | Documented | Research environment only | Not yet run |
| E-11 | BeatNet: CC-BY-4.0 per the repo; streaming, real-time, online, and offline modes; depends on madmom and pins numba 0.54.1 | [GitHub](https://github.com/mjhydri/BeatNet); [PyPI](https://pypi.org/pypi/BeatNet/1.1.3/json) | Documented | Research environment only | Not yet run |
| E-12 | numpy 2.5, scipy 1.18, and librosa 1.0 require Python 3.12 or newer | [numpy](https://pypi.org/project/numpy/), [scipy](https://pypi.org/project/scipy/), [librosa](https://pypi.org/project/librosa/) on PyPI, 16 Oct 2026 | Documented | CI | Not yet run |
| E-13 | Antigravity reads `AGENTS.md` (root and subdirectories) and `.agents/rules/*.md` (which need a `trigger` in frontmatter). Files over 24,000 bytes are truncated. | [Antigravity docs — Rules](https://antigravity.google/docs/rules) | Documented | Confirm the agent follows `AGENTS.md` | Not yet run |
| E-14 | The PyPI name `roobotto` was unclaimed on 16 Oct 2026 | PyPI | Documented | Before M8 | Not yet run |
| E-15 | numba 0.54.1 supports Python 3.7–3.9; macOS on Apple Silicon has unofficial support only | [PyPI — numba 0.54.1](https://pypi.org/project/numba/0.54.1/) | Documented | `research-b` setup, half-day cap | Not yet run |
| E-16 | A macOS Aggregate Device uses one device as the clock source (choose the most reliable), with drift correction enabled for the others | [Apple support — Aggregate Devices](https://support.apple.com/en-us/102171) | Documented | T-07 marker test | Not yet run |
| E-17 | The sounddevice callback reports the ADC capture time of the first input sample, the callback invocation time, and overflow and underflow flags. These times are relative, with an unspecified origin. | [python-sounddevice — Streams](https://python-sounddevice.readthedocs.io/en/latest/api/streams.html) | Documented | T-07 | Not yet run |
| E-18 | MIDI Monitor is a free macOS app that displays MIDI going in and out of the Mac, including what is sent to destinations | [Snoize MIDI Monitor](https://www.snoize.com/MIDIMonitor/) | Documented | First-time verification | Not yet run |
| E-19 | SendMIDI is a command-line MIDI sender, installable with Homebrew, licensed GPL-3.0 (an external bench tool, not a dependency) | [GitHub — SendMIDI](https://github.com/gbevin/SendMIDI) | Documented | First-time verification | Not yet run |

Claims carried over from the council report without being re-checked, such as the prior-art papers, Ableton Tempo Follower, and the licenses of Demucs, Whisper, and sherpa-onnx, remain **[verify]** until someone confirms them against a primary source.

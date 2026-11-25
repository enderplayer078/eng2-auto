# ENG2 Auto (`learn.eltngl.com`)

Automated Selenium exercise solver for **National Geographic Learning (NGL) Spark** assignments (`Avallain Buckminster` interactive player, *Keynote 3* / ENGLISH 2).

## How It Works
1. **Isolated Browser Session (`account_session.py`)**:
   - Launches Headless Chrome with an account-isolated profile directory and signs in to `https://learn.eltngl.com/dashboard/assignments`.
2. **Assignment Scanner & Paced Runner (`run_account.py`)**:
   - Discovers active *Keynote 3* assignments on the student dashboard, skips already completed (`100%`) or exhausted-attempt activities, handles retakes cleanly, and distributes configurable natural pacing across remaining activities with anti-idle heartbeats and clean xAPI duration flushing.
3. **Live Interaction Solver (`solver.py`)**:
   - Inspects the live Avallain Buckminster interaction engine inside the activity iframe and solves all 10 exercise interaction types automatically:
     - Multiple Choice Single Answer (`ISRadiobuttonModel`)
     - Multiple Choice Multiple Answer (`ISCheckboxModel`)
     - Inline Choice / Dropdown (`ISDropdownModel`)
     - Fill in the Blank (`ICTextGap`)
     - Drag & Drop / Gap Match (`GapMatchInteraction`)
     - Crossword Puzzle (`ICCrossword`)
     - Order / Sequencing (`ShuffleModel`)
     - Highlight / Marking (`IMMarkingBlock`)
     - Text Correction (`ICTextCorrectionReplace`, `ICTextCorrectionSentence`)
     - Matching with Lines (`LLModel`)
4. **Pre-Submit 100% Guard (`answer_guard.py`)**:
   - Checks the live engine state before clicking Submit to confirm `100%` accuracy (`revealed == 0`, zero unanswered or wrong items).
5. **Parallel Cloud Worker (`cloud_worker.py`)**:
   - Headless queue worker using atomic ETag CAS job claiming and in-memory RSA-OAEP credential decryption for on-demand GitHub Actions execution ([.github/workflows/worker.yml](.github/workflows/worker.yml)).

## Local Usage
```bash
pip install -r requirements.txt
python3 run_account.py <email>
```

#!/bin/zsh
cd "${0:A:h}"
python3 ./music_state.py start --icloud --port 8766
read

# Improvement loop

A scheduled Claude session improves DB4AI every few hours. Only one session works at a time.

Each run:

1. **Lock.** `scripts/loop-lock.sh acquire <name>`. If it says `held`, stop: another session is working.
2. **Sync.** `git checkout main && git pull --ff-only`. Read `QUESTIONS.md` for answers from the owner.
3. **Pick.** Take the first unchecked item in `ROADMAP.md` that isn't blocked on an open question.
4. **Grill.** Use the `/grill-me` skill if installed; otherwise follow its method yourself. Walk the item's
   design questions one at a time, give each a recommended answer, and resolve it from the code where
   possible. Record the questions and answers in `DECISIONS.md` under a dated heading. A question that only
   the owner can answer (product direction, deleting data, paid services, external accounts) goes in
   `QUESTIONS.md`. That item is then skipped until it is answered.
5. **Build.** Implement the item with tests. Keep the whole suite green (`.venv/bin/pytest`).
6. **Ship.** If every test passes, commit to `main` and push. If not, push to a `loop/<date>` branch and
   note it in `DECISIONS.md`. Tick the item in `ROADMAP.md`.
7. **Unlock.** `scripts/loop-lock.sh release`, always, even after a failure.

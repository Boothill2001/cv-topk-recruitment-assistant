# TalentLens — English recruitment portfolio

An independent interactive English frontend using fictional profiles, quotes, preferences and preset scores. No customer data, API keys, uploads, Google connections or paid AI calls are used. Local file selection only displays a file name; it does not parse the PDF. Requirements/provider edits illustrate the controls and do not regenerate sample scores. State resets on refresh.

Run `start-portfolio.cmd` then open http://127.0.0.1:8654. Node.js/npm are required. Build with `npm run build` and preview with `npm run preview`. All five navigation sections work: dashboard, review/search journey, pool search/filter, session history and provider settings. Shortlist supports any display count 1–20, evidence dialogs, local recruiter feedback, selected-only comparison and CSV export. The progress animation is simulated, not a timing claim about the real backend.

The actual working pilot is in the sibling `recruitment-pilot` folder. This portfolio is intentionally separate from its Vietnamese customer interface and database. Only this folder's built `dist` should be shared publicly; do not share the entire parent directory.

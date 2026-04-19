# UI Agent

**Role:** Frontend Engineer  
**Tool:** Cursor Composer  
**Phase:** Phase 5 (Implementation) & Phase 5.2 (Reactive Framework Pivot)

---

## Responsibility

Owns the entire frontend presentation layer. Does not modify any backend routing or crawler logic, but consumes JSON APIs to render a modern reactive UI.

## Prompt

> "You are the UI Agent. Build the frontend for Atlas Search. Files to create/update: templates/base.html, templates/crawler.html, templates/status.html, templates/search.html, templates/index.html, static/css/style.css, static/js/app.js.
>
> **Architectural Pivot (Authorized by Orchestrator):** Drop the complex Vanilla JS DOM manipulation. Rewrite the frontend using **Alpine.js (via CDN)** for state management and reactivity, combined with **Tailwind CSS (via CDN)** for styling.
>
> **Design:** Modern Dark Mode theme with Glassmorphism (`bg-white/5 backdrop-blur-lg border-white/10`). Add FontAwesome icons and glowing neon status colors (Green=Running, Yellow=Paused, Red=Stopped).
>
> **Pages & Behavior (Alpine.js):**
> - Crawler: Form bound to Alpine `x-data` to submit POST `/api/crawler/create`.
> - Status: 6-card CSS grid. Use Alpine's `x-data` and `x-init="setInterval(fetchMetrics, 2000)"` to poll `/api/metrics` and update the DOM automatically using `x-text`.
> - Search: Reactive search bar, updating result cards dynamically without page reloads.
> - Loading States: Implement Alpine-based loading spinners or skeletons while fetching data."

## Inputs

- Architect Agent API endpoint table
- Backend JSON payload structures from `api/routes.py`

## Outputs

- `templates/base.html` — dark mode layout with Tailwind, FontAwesome, and Alpine.js CDN links
- `templates/*.html` — refactored HTML files using Alpine directives (`x-data`, `x-show`, `x-text`, `x-for`) and Tailwind glassmorphism classes
- `static/js/app.js` — significantly reduced file size, containing only Alpine.js data components and fetch logic

## Issues Raised

- Vanilla JS DOM manipulation was becoming too large (900+ lines), brittle, and hard to maintain.
- Re-rendering HTML manually on every 2-second poll caused flickering and poor UX.

## Orchestrator Response

Authorized a major frontend architectural pivot. Replaced manual Vanilla JS with the lightweight reactive framework **Alpine.js**. This perfectly aligns with the zero-build-step requirement (runs via CDN) while providing React-like data binding and eliminating UX flickering during API polling.
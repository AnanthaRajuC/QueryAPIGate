# Contributing

> **Not accepting outside code contributions at the moment.** Bug reports, questions and suggestions are welcome as
> issues; security problems go through [SECURITY.md](SECURITY.md). The pull-request process below describes how it
> would work if that changes, and any code accepted would be licensed under the project's terms (see
> [LICENSE](LICENSE)). Please do not spend time on a pull request without agreeing it with the maintainer first.

When contributing to this repository, please first discuss the change you wish to make via issue,
email, or any other method with the owners of this repository before making a change. 

Please note we have a code of conduct, please follow it in all your interactions with the project.

## Pull Request Process

1. Ensure any install or build dependencies are removed before the end of the layer when doing a 
   build.
2. Update the README.md with details of changes to the interface, this includes new environment 
   variables, exposed ports, useful file locations and container parameters.
3. Increase the version numbers in any examples files and the README.md to the new version that this
   Pull Request would represent. The versioning scheme we use is [SemVer](http://semver.org/) - see
   [CHANGELOG.md](CHANGELOG.md#versioning-and-compatibility) for what's covered by it and this project's
   pre-1.0 policy.
4. You may merge the Pull Request in once you have the sign-off of two other developers, or if you 
   do not have permission to do that, you may request the second reviewer to merge it for you.

## Code of Conduct

### Our Pledge

In the interest of fostering an open and welcoming environment, we as
contributors and maintainers pledge to making participation in our project and
our community a harassment-free experience for everyone, regardless of age, body
size, disability, ethnicity, gender identity and expression, level of experience,
nationality, personal appearance, race, religion, or sexual identity and
orientation.

### Our Standards

Examples of behavior that contributes to creating a positive environment
include:

* Using welcoming and inclusive language
* Being respectful of differing viewpoints and experiences
* Gracefully accepting constructive criticism
* Focusing on what is best for the community
* Showing empathy towards other community members

Examples of unacceptable behavior by participants include:

* The use of sexualized language or imagery and unwelcome sexual attention or
advances
* Trolling, insulting/derogatory comments, and personal or political attacks
* Public or private harassment
* Publishing others' private information, such as a physical or electronic
  address, without explicit permission
* Other conduct which could reasonably be considered inappropriate in a
  professional setting

### Our Responsibilities

Project maintainers are responsible for clarifying the standards of acceptable
behavior and are expected to take appropriate and fair corrective action in
response to any instances of unacceptable behavior.

Project maintainers have the right and responsibility to remove, edit, or
reject comments, commits, code, wiki edits, issues, and other contributions
that are not aligned to this Code of Conduct, or to ban temporarily or
permanently any contributor for other behaviors that they deem inappropriate,
threatening, offensive, or harmful.

### Scope

This Code of Conduct applies both within project spaces and in public spaces
when an individual is representing the project or its community. Examples of
representing a project or community include using an official project e-mail
address, posting via an official social media account, or acting as an appointed
representative at an online or offline event. Representation of a project may be
further defined and clarified by project maintainers.

### Enforcement

Instances of abusive, harassing, or otherwise unacceptable behavior may be
reported by contacting the project team at arcswdev@gmail.com. All
complaints will be reviewed and investigated and will result in a response that
is deemed necessary and appropriate to the circumstances. The project team is
obligated to maintain confidentiality with regard to the reporter of an incident.
Further details of specific enforcement policies may be posted separately.

Project maintainers who do not follow or enforce the Code of Conduct in good
faith may face temporary or permanent repercussions as determined by other
members of the project's leadership.

### Attribution

This Code of Conduct is adapted from the [Contributor Covenant][homepage], version 1.4,
available at [http://contributor-covenant.org/version/1/4][version]

[homepage]: http://contributor-covenant.org
[version]: http://contributor-covenant.org/version/1/4/

## Development setup

~~~bash
git clone https://github.com/AnanthaRajuC/QueryAPIGate.git && cd QueryAPIGate
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

ruff check .
mypy queryapigate
python -m unittest discover -s tests -t .
~~~

`tests/test_integration.py` runs against real MySQL, PostgreSQL, ClickHouse and H2 servers when the matching
`QUERYAPIGATE_IT_*` environment variables are set (see the file header); otherwise those tests are skipped. CI runs them
against service containers. `tests/test_sql_guard_fuzz.py` property-tests the SQL guard and parameter binder with
[Hypothesis](https://hypothesis.readthedocs.io/); a failure there prints a minimal reproducing example.

`mypy` is configured for gradual typing (`[tool.mypy]` in `pyproject.toml`) - the codebase has no type hints yet, so
it only catches genuine static errors, not missing annotations. `coverage run -m unittest discover -s tests -t .`
followed by `coverage report` shows local coverage; CI uploads it to [Codecov](https://codecov.io/gh/AnanthaRajuC/QueryAPIGate).

### Console (frontend)

The QueryAPIGate Console - the admin UI at `/console` - lives in `frontend/` (React + TypeScript + Vite; see
[ADR 0001](documentation/adr/0001-console-and-management-api.md)). **Backend work never needs Node.js:** without a
build, `/console` shows a short "not built" page, and everything it does is plain API (`/docs`). To see the UI from a
source checkout, build it once (below) or run the Docker image.

To work on it (Node.js 22, see `frontend/.nvmrc`), run the backend as usual, then in a second terminal:

~~~bash
cd frontend
npm ci
npm run dev        # http://localhost:5173/console/ - API calls are proxied to http://127.0.0.1:5000
npm test           # also: npm run lint, npm run typecheck, npm run format:check
npm run build      # writes queryapigate/console_dist/, served by the backend at /console
~~~

The Console's TypeScript types are generated from `frontend/openapi.json`, a committed copy of the backend's OpenAPI
description. **If you change `queryapigate/openapi.py`, regenerate it** - Python only, no Node needed:

~~~bash
python frontend/scripts/dump_openapi.py
~~~

The backend test suite fails while the two disagree, and also checks that every route and method is documented.

End-to-end tests drive the built Console in Chromium against a real server - a throwaway home with the example APIs
(`frontend/e2e/`, run in CI on every push):

~~~bash
cd frontend
npm run build
npx playwright install chromium   # once
npm run e2e                       # set QUERYAPIGATE_BIN=../.venv/bin/queryapigate if it isn't on PATH
~~~

Many comments in `frontend/src` cite the function of `queryapigate/ui.py` a screen was ported from - the hand-written
admin page the Console replaced. It was removed after the migration; read it in git history (`git show
<commit>^:queryapigate/ui.py`, where `<commit>` is the one that deleted it).

### Docs site

The docs site (deployed to GitHub Pages) is built from this README and `documentation/` - there is nothing to edit
under `docs/` itself, each file there is a one-line include (see `mkdocs.yml`) pulling in the real document. Preview
it locally after changing any `.md` file:

~~~bash
pip install -e ".[docs]"
mkdocs serve
~~~

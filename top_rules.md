# Top Rules

The following project rules apply to this service and to the document suite. Source: top_rules.md — that file must not be changed.

**Rule 1.** You may not change anything in top_rules.md.

**Rule 2.** top_rules.md sets project and limitations. The following files must exist where required: readme.md (deployment plan); progress.md (progress tracking); newtask.txt (debugging focus); changed_files.md (tracking edited files). Create any of these if they do not exist, or ensure the deployment plan includes a step to create them.

**Rule 3.** readme.md contains the deployment plan numbered as 1.a, 1.b, etc.

**Rule 4.** progress.md lists progress after each step in the deployment plan (readme.md), including detailed results of tests created for any coded function. Use pytest. Use the same numbering as readme.md. Do not suggest advancing before all tests are cleared. If tests are blocked (e.g. by environment or database), document the blocker in progress.md and suggest how to unblock. After each step, check that all rules are followed, update progress.md, and include a list of the tests deployed and the result of each test.

**Rule 5.** You maintain your own postgresql database

**Rule 6.** This is a service that relies on gateway.pdhc

**Rule 7.** Use virtual environment where applicable. Make sure to be selfstanding and not interefere with other repos.

**Rule 8.** The application will need API keys: include suggested rules (storage, rotation, expiry, revocation) and procedures and maintenance in the deployment plan (readme.md).

**Rule 9.** When appropriate, create a script that tests all API endpoints according to the capability statement.

**Rule 10.** The local database is based on FLASK and PostgreSQL and is localhost to begin with. Later it will be deployed on the same server as the other repos in the PDHC series.

**Rule 11.** All results of tests etc. are stored in ./results/<timestamp>results/ (ISO-8601 UTC; e.g. 2026-02-19T14-30-00Z_results).

**Rule 12.** You have non sudo ssh/sco to miserver@192.168.1.154 /usr/local/www/dashboard.pdhc. Ask before each operation.

**Rule 13.** (Later in project.) tickets are presented in the ~/T7_sidewinder/tickethandler

**Rule 14.** Maintain GIT structure separate for this repo

**Rule 15.** Put priority on being fully compliant with FHIR 5. FHIR compliance is enforced for API schema, DB model, capability statement and validation layer.

**Rule 16.** Assume ownership of ports 9026–9029 on localhost. Kill all (9026–9029) before starting repo. Use only those ports. (se the database on localhost:9026). Starting the database and other applications must be collected in a single bash script (./start.sh), activate venv, start the DB and app; on Ctrl+C gracefully shut down and deactivate. See to that docker is started properly or is running when activating the application. Use the same protnumbers later on the server.

**Rule 17.** Note in changed_files.md all edited files from now on, with full path.

**Rule 18.** For robustness: all internal traffic should be guided by references to GUIDs whenever possible. All matching of activities/transactions/goals must use GUID, not ID. Frontend communication is based on GUIDs. Backend always refers to GUIDs.

**Rule 19.** The operator does all sudo editing on the web application. You are allowed to do graceful restart of the present repo also on the server.

**Rule 20.** Create a script that tests all API endpoints according to the FHIR capability statement.

**Rule 21.** Keep the created app in a separate folder including its venv and database. Make sure to update the requirements.txt file with the dependencies of the app. Keep the root clean.

**Rule 22.** The future implementation on the server is fragile and all precaution must be taken to prevent disturbance of other services in the reverse proxy

**Rule23** the .env must be fully prepared and boot strap SU user must be possible to create in the first implementation on the server (macmini). Development is done on tha local MAC.

**Rule24** The repo must be under SSO in phase analysis and limited to only patients of the organisation of the user. Note that admin sees all. Full log of all operations. 

**Rule 25** see to that technical description and user manual are kept also.


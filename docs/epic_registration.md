# Register With Epic

Screen labels checked **September 21, 2026**; Epic may change the forms.

Return to [setup](../SETUP.md) for installation, local credentials, and syncing.

## 1. Request a developer account

On [Epic on FHIR](https://fhir.epic.com/), choose **Sign Up**, or
**Request an account** from the sign-in page. Both lead to
[Sign Up to Access](https://fhir.epic.com/Developer/Index).
This is a developer account, separate from your hospital's MyChart login.

Enter your name and country. For a personal project, use your own name under
**Company Name** and your email under **Company Email**. Selecting United States
also requires your **Address**, **City**, **State**, and **Zip Code**.
**Phone** and **Website** are optional; leave Website blank if you don't have one.

An agent can help fill the form; complete the CAPTCHA and choose **Submit** yourself.
Follow any verification instructions Epic sends you.

Once you have access, choose **Login > Log in with your Epic on FHIR account**.
The other login choice is for existing Epic UserWeb accounts.

## 2. Create the app

Open **Build Apps > Create**. Start with these fields:

| Field | Setting for MyChart Sync |
| --- | --- |
| Application Name | `MyChart Sync` or another descriptive name |
| Application Audience | **Patients**; this reveals the patient-app settings |
| Automatic Client Distribution | **USCDI v3** |
| Endpoint URI | **Add Another URI**, choose `https://`, enter `localhost:8080/callback` in the adjoining box |
| Is Confidential Client | Check |
| Requires Persistent Access | Check; appears after selecting confidential client |
| Can Register Dynamic Clients | Leave unchecked |

The complete callback must be exactly `https://localhost:8080/callback`, with the
scheme entered only once. Persistent access enables refresh tokens for later syncs.

**Automatic Client Distribution > USCDI v3** enables auto-download and narrows
the API list, but does **not** select the APIs for you.

Confidential-client settings also show JWK Set URLs and a **Sandbox Client Secret**.
MyChart Sync uses hospital client secrets. Leave JWK Set URLs blank; obtain hospital
secrets in step 5. A Sandbox Client Secret is only for Epic's test environment.

## 3. Select the APIs

Under **Incoming APIs**, click an entry in **Available**, then **Add selected**
(`>>`) to move it to **Selected**. **Remove selected** (`<<`) reverses that.

For broad record access, select all patient-readable **R4 Read and Search** APIs
eligible for USCDI v3 automatic distribution. Check against Epic's current
[eligible API list](https://fhir.epic.com/Documentation?docId=epicidtypes).
**Search is read-only** and is needed to list records.

Names have important variants. Examples include:

- `Patient.Read (Demographics) (R4)` and its Search counterpart.
- `Observation.Read (Labs) (R4)`, plus separate Vital Signs and Social History APIs.
- `DocumentReference.Read (Clinical Notes) (R4)` and separate Generated CDAs APIs.
- `Binary.Read (Clinical Notes) (R4)` for document contents.
- Separate Patient Chart and Outside Record entries for several resource types.

Leave DSTU2, STU3, and Create/Update/Delete operations unselected.
Check each entry's version even when using the search box. An agent can handle the
repetition, then review the complete **Selected** list with you before saving.

Selecting more APIs permits access; it doesn't add new downloads to this tool.
The [client's supported resources](../src/health_sync/fhir/client.py) and the
hospital's available data determine what gets downloaded.

## 4. Ready the app for production

Save the app and complete the remaining registration fields:

| Field | Suggested entry for personal use |
| --- | --- |
| SMART on FHIR Version | **R4** |
| Summary | Downloads my medical records to local Markdown and JSON files. |
| Description | Connects to my MyChart accounts with my authorization and saves records on my computer for personal review and analysis. |
| Intended Purposes | **Individuals' Access to their EHI** |
| Intended Users | **Individual/Caregiver** |
| Terms and Conditions Secure URL | An HTTPS link to your reviewed data-use disclosure |

Adapt the [example disclosure](terms.html) to your use and publish it at an HTTPS
URL for the terms field. Answer the **Data Use Questionnaire** to match your setup,
including any cloud agents or file-sync services you use. Review and accept the
terms yourself.

**SMART Scope Version** is separate from the FHIR version. MyChart Sync requests
[SMART v1-style scopes](../src/health_sync/auth/smart_auth.py); R4 does not mean
you need SMART v2. Preserve working scope and FHIR ID settings in an existing app.

In `config/app.json`, copy **Client ID** into `client_id` and **Non-Production
Client ID** into `sandbox_client_id`. Review the API list and callback URL, then
mark the app **Ready for Production**. Epic restricts edits afterward.

## 5. Enable each hospital

Return to **Build Apps** and choose your app's **Review & Manage Downloads**.
This opens **Manage keys**. Search for your hospital and inspect **Status**.
**Not responded** means you still need to act, not wait.

Choose **Activate for Non-Production**, then **Activate for Production**.
If production is disabled, complete non-production activation first.
Follow the prompts and provision each environment's secret using Epic's
[credential instructions](https://fhir.epic.com/Documentation?docId=epicidtypes).
Save secrets directly in the local files described in [setup](../SETUP.md#secrets-and-tokens).

Verify production activation for **every hospital** before waiting. Epic allows
up to 12 hours for distribution after enablement; this is separate from creating
your developer account. Then continue with [hospital authentication](../SETUP.md#3-connect-each-hospital).

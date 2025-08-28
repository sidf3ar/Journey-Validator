import json
import re
import io
import os
import uuid
import pandas as pd
from collections import defaultdict, deque
from flask import Flask, render_template,render_template_string, request, redirect, url_for, flash, send_file, session
from flask_session import Session

# --- Initialize the Flask App ---
app = Flask(__name__)
app.config['SECRET_KEY'] = 'AJOAPP'
app.config["SESSION_PERMANENT"] = False
app.config["SESSION_TYPE"] = "filesystem"
os.makedirs(os.path.join(app.root_path, 'flask_session'), exist_ok=True)
Session(app)

# --- Core Logic ---



#region --- Helper Functions for Reporting and Validation ---

def process_ajo_journey(json_data: str) -> list[tuple[str, pd.DataFrame]]:
    """Extracts detailed journey information into multiple DataFrames for Excel reporting."""
    try:
        journey = json.loads(json_data)
        nodes = journey.get('ui', {}).get('nodes', [])
        meta_fields = ['orgID', 'orgName', 'sandboxName', 'uid', 'name', 'journey', 'journeyVersion', 'state', 'reviewState', 'description', 'priority', 'isLatest', 'isBatch', 'inTest', 'keyNamespace', 'reentrancePolicy', 'validationStatus']
        metadata = journey.get('metadata', {})
        meta_dict = {field: journey.get(field, '') for field in meta_fields}
        meta_dict.update({'createdAt': metadata.get('createdAt', ''), 'createdBy': metadata.get('createdBy', ''), 'lastModifiedAt': metadata.get('lastModifiedAt', ''), 'lastModifiedBy': metadata.get('lastModifiedBy', ''), 'lastDeployedAt': metadata.get('lastDeployedAt', ''), 'lastDeployedBy': metadata.get('lastDeployedBy', '')})
        meta_df = pd.DataFrame(list(meta_dict.items()), columns=['Field', 'Value'])
        
        read_audience, conditions, wait_steps, custom_actions = [], [], [], []
        if isinstance(nodes, list):
            for node in nodes:
                node_type, data = node.get('type', ''), node.get('data', {})
                if node_type == 'segmentTrigger':
                    read_audience.append({'Node ID': node.get('id', ''), 'Label': node.get('label', ''), 'Segment Name': data.get('segment', {}).get('name', ''), 'Segment ID': data.get('segment', {}).get('id', ''), 'Throttling Rate': data.get('throttlingRatePerSec', ''), 'Namespace': data.get('namespaceId', '')})
                elif node_type == 'condition':
                    for condition_path in data.get('conditions', []):
                        conditions.append({'Node ID': node.get('id', ''), 'Workflow Label': data.get('label', ''), 'Condition Name': condition_path.get('name', ''), 'Condition Type': condition_path.get('conditionType', ''), 'Logic': condition_path.get('parameterizedExpression', {}).get('plainText', '')})
                elif node_type == 'timer':
                    wait_steps.append({'Node ID': node.get('id', ''), 'Label': data.get('label', ''), 'Wait Type': data.get('type', ''), 'Delay Duration': data.get('delay', ''), 'Custom Expression': data.get('parameterizedExpression', {}).get('plainText', '')})
                elif node_type == 'action' and node.get('subtype') == 'custom' or node_type == 'action' and node.get('name') == 'updatePlatformAction' :
                    params = "; ".join([f"{p.get('label', '')}: {p.get('parameterizedExpression', {}).get('plainText', '')}" for p in data.get('paramMappings', [])])
                    custom_actions.append({'Node ID': node.get('id', ''), 'Label': data.get('label', ''), 'Action Name': node.get('label', ''), 'Action UID': data.get('uid', ''), 'URL Path': data.get('urlAdditionalPath', {}).get('plainText', ''), 'Parameters': params})
        
        return [('Journey Meta Data', meta_df), ('Read Audience', pd.DataFrame(read_audience)), ('Conditions', pd.DataFrame(conditions)), ('Wait Steps', pd.DataFrame(wait_steps)), ('Custom Actions', pd.DataFrame(custom_actions))]
    except (json.JSONDecodeError, TypeError, AttributeError) as e:
        return [('Error', pd.DataFrame([{'Message': f'Could not process JSON for reporting: {e}'}]))]

def auto_adjust_column_width(sheet):
    for col in sheet.columns:
        max_length = 0
        column = col[0].column_letter
        for cell in col:
            try:
                if cell.value and len(str(cell.value)) > max_length:
                    max_length = len(str(cell.value))
            except: pass
        adjusted_width = max_length + 2
        sheet.column_dimensions[column].width = adjusted_width

def replace_ws_variants_re(text):
    # re.IGNORECASE makes the pattern case-insensitive
    # 'WS' will match 'WS', 'ws', 'Ws', 'wS'
    return re.sub(r"WS", "WF", text, flags=re.IGNORECASE)

def generate_corrected_label(original_label, new_number, prefix):
    prefixes_to_check = ("WS_", "ws_", "Ws_")
    if prefix == 'WF' and original_label.startswith(prefixes_to_check):
       original_label = replace_ws_variants_re(original_label)
    prefix_pattern = re.compile(r'^' + re.escape(prefix) + r'_\d+(_)?', re.IGNORECASE)
    descriptive_part = prefix_pattern.sub('', original_label).strip()
    return f"{prefix}_{new_number}_{descriptive_part}"

#endregion

#region --- Unified Validation and Correction Generation ---

def apply_all_transformations(original_path_name: str, path_type: str, journey_suffix: str, replacements: list, league_club_code: str) -> str:
    """Applies all necessary transformations to a path name."""
    # This is a simplified version for demonstration.
    corrected = original_path_name
    for old, new in replacements:
        corrected = corrected.replace(old, new)
    if not corrected.endswith(journey_suffix):
        corrected += journey_suffix
    if path_type == 'opposite' and not corrected.startswith(league_club_code + '_Non'):
        corrected = league_club_code + '_Non' + corrected
    return corrected

def validate_and_generate_all_corrections(data: dict, journey_suffix: str, replacements: list, triggers: list, league_club_code: str) -> tuple[list, pd.DataFrame, dict]:
    nodes, edges = data.get("ui", {}).get("nodes", []), data.get("ui", {}).get("edges", [])
    all_corrections = []
    report_data = []

    summary = {
        'suffix': {'passed': 0, 'total': 0}, 
        'opposite_prefix': {'passed': 0, 'total': 0},
        'flow_sequence': {'passed': 0, 'total': 0},
        'duplicate_node_labels': {'found': 0, 'total': 0}, # Renamed 'total_nodes_checked' to 'total' for consistency
        'duplicate_path_names': {'found': 0, 'total': 0}    # Renamed 'total_paths_checked' to 'total' for consistency
    }
    journey_leadge_code = league_club_code + '_Non'

    # --- Duplicate Tracking (Populated in a single pass over relevant elements) ---
    # These will be used to detect duplicates after all elements are processed
    potential_duplicate_node_labels = defaultdict(list) 
    potential_duplicate_path_names = defaultdict(list)  

    # --- Populate total counts for duplicates and initial seen lists ---
    node_map = {node['id']: node for node in nodes} # Ensure node_map is available early

    for node in nodes:
        node_id = node['id']
        node_type = node.get("type")
        label = node.get("data", {}).get("label", "").strip()

        if node_type in ["condition", "timer"] and label:
            summary['duplicate_node_labels']['total'] += 1
            potential_duplicate_node_labels[label].append(node_id)
        
        if node_type == 'condition':
            for path in node.get('data', {}).get('conditions', []):
                if path.get('conditionType') not in ['percentage']:
                    original_path_name = path.get('name', '')
                    if original_path_name:
                        summary['duplicate_path_names']['total'] += 1
                        potential_duplicate_path_names[original_path_name].append((node_id, original_path_name))

    # --- Flow and Sequence Validation ---
    if nodes and edges:
        adj = defaultdict(list)
        for edge in edges:
            src, tgt = edge.get("source", {}).get("elementId"), edge.get("target", {}).get("elementId")
            if src and tgt: adj[src].append(tgt)
        
        start_node_id = next((e.get("target", {}).get("elementId") for e in edges if e.get("source", {}).get("elementId") == "start"), None)
        
        if start_node_id:
            q, visited, wf_c, ws_c = deque([start_node_id]), set(), 1, 1
            wf_p, ws_p = re.compile(r'^WF_(\d+)'), re.compile(r'^WS_(\d+)', re.IGNORECASE)
            temp_flow_results = []
            
            while q:
                curr_id = q.popleft()
                if curr_id in visited: continue
                visited.add(curr_id)
                node = node_map.get(curr_id)
                if not node: continue
                
                node_type, label = node.get("type"), node.get("data", {}).get("label", "").strip()
                
                if node_type not in ["condition", "timer"]:
                    for neighbor in adj.get(curr_id, []):
                        if neighbor not in visited: q.append(neighbor)
                    continue
                
                res_item = {"id": curr_id, "label": label, "status": "OK", "suggestion": "In sequence.", "corrected_label": label}
                
                if node_type == "condition":
                    if not (m := wf_p.search(label)) or int(m.group(1)) != wf_c:
                        corr_label = generate_corrected_label(label, wf_c, "WF")
                        res_item.update({"status": "Error", "suggestion": f"Expected WF_{wf_c}", "corrected_label": corr_label})
                        all_corrections.append({'type': 'node_label', 'id': curr_id, 'original_label': label, 'corrected_label': corr_label})
                    wf_c += 1
                elif node_type == "timer":
                    if not (m := ws_p.search(label)) or int(m.group(1)) != ws_c:
                        corr_label = generate_corrected_label(label, ws_c, "WS")
                        res_item.update({"status": "Error", "suggestion": f"Expected WS_{ws_c}", "corrected_label": corr_label})
                        all_corrections.append({'type': 'node_label', 'id': curr_id, 'original_label': label, 'corrected_label': corr_label})
                    ws_c += 1
                
                temp_flow_results.append(res_item)
                for neighbor in adj.get(curr_id, []):
                    if neighbor not in visited: q.append(neighbor)

            for n_id, lbl in {n['id']: n.get('data', {}).get('label', '').strip() for n in nodes if n.get('type') in ['condition', 'timer'] and n['id'] not in visited}.items():
                temp_flow_results.append({"id": n_id, "label": lbl, "status": "Error", "suggestion": "Unreachable node.", "corrected_label": "N/A"})

            sorted_flow_results = sorted(temp_flow_results, key=lambda x: (x['status'] != 'OK', x['label']))
            summary['flow_sequence']['total'] = len(sorted_flow_results)
            for res in sorted_flow_results:
                is_pass = res['status'] == 'OK'
                if is_pass:
                    summary['flow_sequence']['passed'] += 1
                
                report_data.append({
                    "Check Type": "Flow and Sequence Validation",
                    "Element": f"Node: '{res['label']}' (ID: {res['id']})", 
                    "Value Checked": res['label'],
                    "Status": "PASS" if is_pass else "FAIL",
                    "Details": res['suggestion']
                })
    opresorceName = {}
    checked_node_labels = set()
    # --- Naming and Suffix Validation ---
    for node in (n for n in nodes if n.get('type') == 'condition'):
        for path in node.get('data', {}).get('conditions', []):
            if path.get('conditionType') not in ['percentage']:
                original_path_name = path.get('name', '')
                path_type = path.get('conditionType', '')
                
                final_corrected_name = apply_all_transformations(original_path_name, path_type, journey_suffix, replacements, league_club_code)
                
                summary['suffix']['total'] += 1
                suffix_pass = original_path_name.endswith(journey_suffix)
                if suffix_pass: summary['suffix']['passed'] += 1
                report_data.append({
                    "Check Type": "Path Suffix", 
                    "Element": f"Path '{original_path_name}' in Node '{node['data']['label']}' (ID: {node['id']})", 
                    "Value Checked": original_path_name, 
                    "Status": "PASS" if suffix_pass else "FAIL", 
                    "Details": f"Expected suffix: '{journey_suffix}'"
                })
                
                is_OpResource = False # This flag is local to each iteration, which is good.

                if path_type == 'resource':
                    node_label = node['data']['label']

                # Ensure the list exists for the node_label
                if node_label not in opresorceName:
                    opresorceName[node_label] = []

                # Append the path name
                opresorceName[node_label].append(original_path_name)

                # Check if the path is in the list (it just was, so this is redundant unless you're checking for duplicates)
                # The more important check is if it starts with journey_leadge_code
                if original_path_name.startswith(journey_leadge_code):
                    is_OpResource = True # Mark this specific path as handled by resource logic
                    
                    # Add the node_label to the set of checked labels
                    checked_node_labels.add(node_label) 

                    report_data.append({
                        "Check Type": "Resource Path Check (Early Exit)",
                        "Element": f"Path '{original_path_name}' in Node '{node['data']['label']}' (ID: {node['id']})",
                        "Value Checked": original_path_name,
                        "Status": "PASS", # Assuming if it starts with journey_leadge_code, it's a pass for resource type
                        "Details": f"Path matches prefix '{journey_leadge_code}'. Node label '{node_label}' marked as checked, skipping opposite check."
                    })

                # Now, in the 'opposite' condition, check if the current node's label is in the 'checked_node_labels' set
                if path_type == 'opposite':
                    node_label = node['data']['label'] # Get the node label for the current iteration

                    # Only proceed with the opposite check if this node_label has NOT been marked as checked
                    if node_label not in checked_node_labels:
                        summary['opposite_prefix']['total'] += 1
                        is_prefix_valid = original_path_name.startswith(journey_leadge_code)
                        if is_prefix_valid:
                            summary['opposite_prefix']['passed'] += 1
                            status = "PASS"
                        else:
                            status = "FAIL"

                        report_data.append({
                            "Check Type": "Opposite Prefix (Standard)",
                            "Element": f"Path '{original_path_name}' in Node '{node['data']['label']}' (ID: {node['id']})",
                            "Value Checked": original_path_name,
                            "Status": status,
                            "Details": f"Expected prefix: '{journey_leadge_code}'"
                        })
                    else:
                        summary['opposite_prefix']['total'] += 1
                        summary['opposite_prefix']['passed'] += 1
                        # This block handles cases where the opposite check is skipped
                        report_data.append({
                            "Check Type": "Opposite Prefix (Skipped)",
                            "Element": f"Path '{original_path_name}' in Node '{node['data']['label']}' (ID: {node['id']})",
                            "Value Checked": original_path_name,
                            "Status": "SKIPPED",
                            "Details": f"Node label '{node_label}' was previously handled by a 'resource' path check."
                        })

                
                if final_corrected_name != original_path_name:
                    all_corrections.append({'type': 'path_name', 'node_id': node['id'], 'original_name': original_path_name, 'corrected_name': final_corrected_name})

    # --- Duplicate Check Reporting and Summary Update ---
    for label, ids in potential_duplicate_node_labels.items():
        if len(ids) > 1:
            summary['duplicate_node_labels']['found'] += 1 
            report_data.append({
                "Check Type": "Duplicate Node Label",
                "Element": f"Node Label: '{label}'",
                "Value Checked": label,
                "Status": "FAIL",
                "Details": f"Duplicate label found for nodes with IDs: {', '.join(ids)}. Please ensure unique labels."
            })

    for name, occurrences in potential_duplicate_path_names.items():
        if len(occurrences) > 1:
            summary['duplicate_path_names']['found'] += 1 
            node_details = [f"Node '{node_map.get(nid, {}).get('data', {}).get('label', 'Unknown')}' (ID: {nid})" for nid, _ in occurrences]
            report_data.append({
                "Check Type": "Duplicate Path Name",
                "Element": f"Path Name: '{name}'",
                "Value Checked": name,
                "Status": "FAIL",
                "Details": f"Duplicate path name found in: {'; '.join(node_details)}. Please ensure unique path names within the journey."
            })

    return all_corrections, pd.DataFrame(report_data), summary

#endregion

#region --- JSON Correction and Transformation ---

def correct_original_json(original_data: dict, corrections: list) -> dict:
    data = json.loads(json.dumps(original_data))
    node_map = {node['id']: node for node in data.get('ui', {}).get('nodes', [])}
    path_map = {(c['node_id'], c['original_name']): c['corrected_name'] for c in corrections if c['type'] == 'path_name'}
    for c in corrections:
        if c['type'] == 'node_label' and (node := node_map.get(c['id'])):
            node['data']['label'] = c['corrected_label']
    for node in node_map.values():
        if 'conditions' in node.get('data', {}):
            for path in node['data']['conditions']:
                if (corr_name := path_map.get((node['id'], path.get('name')))):
                    path['name'] = corr_name
    return data

def transform_json_structure(data: dict) -> dict:
    return {
        "authoringFormatVersion": data.get("authoringFormatVersion", "2.0"),
        "orgId": data.get("orgID") or data.get("orgId", ""),
        "nodes": [n for n in data['ui']['nodes'] if n.get('type') not in ['start', 'end']],
        "edges": [e for e in data['ui']['edges'] if e.get('source', {}).get('elementId') != 'start'],
        "parentJourneyData": {"hasInlineCampaigns": data.get("hasInlineCampaigns", False), "sandboxName": data.get("sandboxName", "")}
    }

#endregion

# --- Helper function for styling ---
def highlight_fails(row):
    """
    Applies a red background to a row if its 'Status' column is 'FAIL'.
    Uses a light red color similar to Bootstrap's 'table-danger' class.
    """
    return ['background-color: #f8d7da'] * len(row) if row.Status == 'FAIL' else [''] * len(row)

#region --- Flask Routes ---

@app.route('/upload', methods=['POST'])
def upload():
 """Handles file upload and processes the JSON file."""
 if 'json_file' not in request.files:
    flash('No file part in the request. Please select a file.', 'error') # 'error' is the category
    return redirect(url_for('index', session='active'))
 
 file = request.files['json_file']
 if file.filename == '':
     flash('No file selected. Please choose a JSON file.', 'error')
     return redirect(url_for('index', session='active'))
 
 try:
     # Parse the uploaded JSON file
     journey_data = json.load(file)
     session['journey_data'] = journey_data  # Store the JSON data in the session
     return redirect(url_for('process_upload_journey'))
 except json.JSONDecodeError:
     flash('Invalid JSON File', 'error')
     return redirect(url_for('index', session='active'))
 
@app.route('/upload', methods=['GET', 'POST'])
def process_upload_journey():
    """Processes the journey JSON and validates it."""
    journey_data = session.get('journey_data')
    if not journey_data:
        return redirect(url_for('index', session='active'))
    
    # Retrieve suffix and league/club code from session or use defaults
    suffix = session.get('suffix_for_report', 'Default_Journey_2024_V1')
    league_club_code = session.get('league_club_code_for_report', 'Default_League')

    try:
        replacements_str = request.form.get('replacements', '')
        replacements = []
        if replacements_str:
            for line in replacements_str.splitlines():
                if '->' in line:
                    parts = line.split('->', 1)
                    if len(parts) == 2 and (find_text := parts[0].strip()):
                        replacements.append((find_text, parts[1].strip()))
        
        triggers_str = request.form.get('opposite_triggers', '')
        triggers = [t.strip() for t in triggers_str.split(',') if t.strip()]

        original_data = journey_data

        journeyOrgName = original_data.get('orgName', '').lower()
        journeySandboxName = original_data.get('sandboxName', '')
        JourneyUniqueId =  original_data.get('uid', '')

        journeyLink = f'https://experience.adobe.com/#/@{journeyOrgName}/sname:{journeySandboxName}/journey-optimizer/journeys/journey/{JourneyUniqueId}'
        corrections, naming_df, summary = validate_and_generate_all_corrections(original_data, suffix, replacements, triggers,league_club_code)
        flow_results = {}
        corrected_original = correct_original_json(original_data, corrections)
        transformed_data = transform_json_structure(corrected_original)

        session['corrected_original_json_str'] = json.dumps(corrected_original, indent=2)
        session['transformed_json_str'] = json.dumps(transformed_data, indent=2)
        session['journey_name'] = original_data.get('name', 'Unknown Journey')       
        session['original_json_str_for_report'] = json.dumps(original_data)       
        session['league_club_code_for_report'] = league_club_code
        session['replacements_for_report'] = replacements
        session['triggers_for_report'] = triggers
        if not naming_df.empty:
            # Use the Styler object to apply conditional formatting
            styler = naming_df.style.apply(highlight_fails, axis=1)

            # Set the table's HTML class attributes and hide the DataFrame index
            styler.set_table_attributes('class="table table-striped table-bordered table-hover"')
            styler.hide(axis="index") # This is the styler's equivalent of index=False

            # Render the styled DataFrame to an HTML string
            report_html = styler.to_html()

            # Inject the class for the table header, as before
            report_html = report_html.replace(
                '<thead>',
                '<thead class="table-primary">'
            )
        else:
            report_html = "<p>No validation issues found.</p>"

            # Note: The 'flow_results' variable is now obsolete since we merged it
            # into the main report DataFrame. I've removed it from the render_template call.
        return render_template('results.html',
                            journey_name=session['journey_name'],
                            summary=summary,
                            # Pass the newly generated HTML to the template
                            naming_results_html=report_html,journey_link=journeyLink)
    except Exception as e:
                flash(f'An unexpected application error occurred: {e}')
               
    

@app.route('/', methods=['GET'])
def index():
    sessionVar = request.args.get('session')
    if sessionVar is None: # Only clear if 'session' parameter is completely absent
        session.clear()
    return render_template('index.html')

@app.route('/process', methods=['POST'])
def process_journey():
    if 'file' not in request.files or not request.files['file'].filename:
        flash('No file selected.')
        return redirect(url_for('index', session='active'))
    file = request.files['file']
    suffix = request.form.get('suffix', '').strip()
    league_club_code = request.form.get('league_club_code','').strip()
    if not suffix:
        flash('Journey Suffix is required.')
        return redirect(url_for('index', session='active'))
    if not league_club_code:
        flash('Leadge or Club Code is required.')
        return redirect(url_for('index', session='active'))
    try:
        replacements_str = request.form.get('replacements', '')
        replacements = []
        if replacements_str:
            for line in replacements_str.splitlines():
                if '->' in line:
                    parts = line.split('->', 1)
                    if len(parts) == 2 and (find_text := parts[0].strip()):
                        replacements.append((find_text, parts[1].strip()))
        
        triggers_str = request.form.get('opposite_triggers', '')
        triggers = [t.strip() for t in triggers_str.split(',') if t.strip()]

        original_data = json.loads(file.read().decode('utf-8'))

        journeyOrgName = original_data.get('orgName', '').lower()
        journeySandboxName = original_data.get('sandboxName', '')
        JourneyUniqueId =  original_data.get('uid', '')

        journeyLink = f'https://experience.adobe.com/#/@{journeyOrgName}/sname:{journeySandboxName}/journey-optimizer/journeys/journey/{JourneyUniqueId}'
        corrections, naming_df, summary = validate_and_generate_all_corrections(original_data, suffix, replacements, triggers,league_club_code)
        flow_results = {}
        corrected_original = correct_original_json(original_data, corrections)
        transformed_data = transform_json_structure(corrected_original)

        session['corrected_original_json_str'] = json.dumps(corrected_original, indent=2)
        session['transformed_json_str'] = json.dumps(transformed_data, indent=2)
        session['journey_name'] = original_data.get('name', 'Unknown Journey')
        session['original_filename'] = file.filename
        session['original_json_str_for_report'] = json.dumps(original_data)
        session['suffix_for_report'] = suffix
        session['league_club_code_for_report'] = league_club_code
        session['replacements_for_report'] = replacements
        session['triggers_for_report'] = triggers
        if not naming_df.empty:
            # Use the Styler object to apply conditional formatting
            styler = naming_df.style.apply(highlight_fails, axis=1)

            # Set the table's HTML class attributes and hide the DataFrame index
            styler.set_table_attributes('class="table table-striped table-bordered table-hover"')
            styler.hide(axis="index") # This is the styler's equivalent of index=False

            # Render the styled DataFrame to an HTML string
            report_html = styler.to_html()

            # Inject the class for the table header, as before
            report_html = report_html.replace(
                '<thead>',
                '<thead class="table-primary">'
            )
        else:
            report_html = "<p>No validation issues found.</p>"

            # Note: The 'flow_results' variable is now obsolete since we merged it
            # into the main report DataFrame. I've removed it from the render_template call.
        return render_template('results.html',
                            journey_name=session['journey_name'],
                            summary=summary,
                            # Pass the newly generated HTML to the template
                            naming_results_html=report_html,journey_link=journeyLink)
    except Exception as e:
                flash(f'An unexpected application error occurred: {e}')
                return redirect(url_for('index', session='active'))

@app.route('/download_excel_report')
def download_excel_report():
    json_data = session.get('original_json_str_for_report')
    suffix = session.get('suffix_for_report')
    replacements = session.get('replacements_for_report', [])
    triggers = session.get('triggers_for_report', [])
    league_club_code = session.get('league_club_code_for_report')
    if not json_data or not suffix:
        flash('Report data has expired.')
        return redirect(url_for('index', session='active'))
    _, validation_df, _ = validate_and_generate_all_corrections(json.loads(json_data), suffix, replacements, triggers,league_club_code)
    processed_dfs = process_ajo_journey(json_data)
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        for sheet_name, df in processed_dfs:
            if not df.empty:
                df.to_excel(writer, sheet_name=sheet_name, index=False)
                auto_adjust_column_width(writer.sheets[sheet_name])
        if not validation_df.empty:
            validation_df.to_excel(writer, sheet_name='Naming Validation', index=False)
            auto_adjust_column_width(writer.sheets['Naming Validation'])
    output.seek(0)
    safe_filename = re.sub(r'[\W_]+', '_', session.get('journey_name', 'Journey'))
    return send_file(output, mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', as_attachment=True, download_name=f'{safe_filename}_Validation_Report.xlsx')

@app.route('/download_corrected_original')
def download_corrected_original():
    json_str = session.get('corrected_original_json_str')
    filename = session.get('original_filename', 'file.json')
    if not json_str:
        flash('File data has expired.')
        return redirect(url_for('index', session='active'))
    return send_file(io.BytesIO(json_str.encode('utf-8')), as_attachment=True, download_name=f"CORRECTED_{filename}", mimetype='application/json')

@app.route('/download_transformed')
def download_transformed():
    json_str = session.get('transformed_json_str')
    filename = session.get('original_filename', 'file.json')
    if not json_str:
        flash('File data has expired.')
        return redirect(url_for('index', session='active'))
    return send_file(io.BytesIO(json_str.encode('utf-8')), as_attachment=True, download_name=f"TRANSFORMED_{filename}", mimetype='application/json')

#endregion
if __name__ == '__main__':
    app.run(debug=True)


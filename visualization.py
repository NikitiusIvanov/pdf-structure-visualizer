import re
import os
import json
import base64
from io import BytesIO
from PIL import Image

import dash
from dash import dcc, html, Input, Output, State, ALL, callback_context
import dash_bootstrap_components as dbc
import fitz  # PyMuPDF for reading PDF pages

# =============================================================================
# Load CSV data for testing the parsing & compilation
# segmentation_results.csv must have columns: paper, parsed_content (JSON string)
# columns_mapping.csv must have columns: paper, mapping (JSON string)
with open('segmentation_results.json', 'r') as f:
    segmentation_results = json.load(f)
with open('columns_mapping.json', 'r') as f:
    columns_mapping = json.load(f)

port = int(os.environ.get("PORT", 8050))

available_papers = list(segmentation_results.keys())

# =============================================================================
# Helper: compile parsed blocks into structured dict
def compile_parsed_content(
        parsed_content, 
        section_headers_mapping, 
        block_link_format="[block:{}]"
):
    """
    Compile parsed blocks into a structured output with 'content' and 'resources'.
    
    Args:
      parsed_content (list[dict]): List of blocks from the PDF parser.
      section_headers_mapping (dict): Mapping from a Section header's text to standardized section name,
                                      or "none" if it should be treated as a subsection marker.
      block_link_format (str): Format string for block link references, e.g. "[block:{}]". 
      
    Returns:
      dict: structured output containing:
            - "content": list of dicts with keys: "section", "section_block", "subsection", "subsection_block", "text"
            - "resources": list of dicts for resources with keys: "reference", "content", "content_type",
                           "description", "content_block", "description_block"
    """
    # Initialize output lists
    content_sections = []
    resources = []
    
    # Default current section and subsection values (for content before first standard header)
    current_section = "section_1"
    current_section_block = ""
    current_subsection = "subsection_1"
    current_subsection_block = ""
    current_text_buffer = []  # to accumulate text (with block links) that belongs to the current (sub)section

    # Counters for resource references (separate from block numbering)
    image_counter = 1
    table_counter = 1
    
    # Keep track of the most recent resource (Picture/Table) for Caption assignment.
    last_resource = None

    def flush_current_text():
        """Flush the current text buffer into a content entry if any text was accumulated."""
        nonlocal current_text_buffer, current_section, current_section_block, current_subsection, current_subsection_block, content_sections
        if current_text_buffer:
            joined_text = " ".join(current_text_buffer)
            content_sections.append({
                "section": current_section,
                "section_block": current_section_block,
                "subsection": current_subsection,
                "subsection_block": current_subsection_block,
                "text": joined_text
            })
            current_text_buffer.clear()

    # Process each block (zero-indexed)
    for idx, block in enumerate(parsed_content):
        # Create the block reference using the provided format string
        block_ref = block_link_format.format(idx)
        block_type = block.get("type", "")
        raw_text = block.get("text") or ""
        block_text = raw_text.strip()

        # --- Process Section header blocks ---
        if block_type == "Section header":
            # Look up the header mapping (you might want to normalize block_text first)
            mapped = section_headers_mapping.get(block_text, None)
            if mapped and mapped.lower() != "none":
                # Standard section header found.
                flush_current_text()
                # Update current section (in lower case) and record its block reference.
                current_section = mapped.lower()
                current_section_block = block_ref
                # Reset subsection values to defaults.
                current_subsection = "subsection_1"
                current_subsection_block = ""
            else:
                # Mapping returns "none" (or mapping not found) so treat as a subsection marker.
                flush_current_text()
                current_subsection = block_text  # Use the header text as the subsection title.
                current_subsection_block = block_ref
            # Do not add the header's text to the text buffer.
        
        # --- Process resource blocks (Picture and Table) ---
        elif block_type in ("Picture", "Table"):
            if block_type == "Picture":
                resource_ref = f"[image:{image_counter}]"
                content_type = "image"
                image_counter += 1
            else:  # Table
                resource_ref = f"[table:{table_counter}]"
                content_type = "table"
                table_counter += 1

            # Create the resource entry.
            resource_entry = {
                "reference": resource_ref,
                "content": block_text,  # for tables: markdown-formatted table; for pictures: later replace with base64 data if needed.
                "content_type": content_type,
                "description": "",  # to be filled in if a Caption is found
                "content_block": idx,       # zero-indexed block number
                "description_block": None
            }
            resources.append(resource_entry)
            last_resource = resource_entry
            # Insert a link for this resource in the joined text.
            current_text_buffer.append(f"{block_ref}{resource_ref}{block_ref}")
        
        # --- Process Caption blocks ---
        elif block_type == "Caption":
            if last_resource is not None:
                # If the last resource has no description assigned yet, assign this caption.
                if not last_resource["description"]:
                    last_resource["description"] = f"{block_ref}{block_text}{block_ref}"
                    last_resource["description_block"] = idx
            else:
                # No resource to attach the caption to; treat as normal text.
                if block_text:
                    current_text_buffer.append(f"{block_ref}{block_text}{block_ref}")
        
        # --- Process all other block types (Title, Text, List item, etc.) ---
        else:
            if block_text:
                current_text_buffer.append(f"{block_ref}{block_text}{block_ref}")
    
    # Flush any remaining text after processing all blocks.
    flush_current_text()
    
    # If no content entries were created (e.g. if parsed content had only resources), add a default one.
    if not content_sections:
        content_sections.append({
            "section": current_section,
            "section_block": current_section_block,
            "subsection": current_subsection,
            "subsection_block": current_subsection_block,
            "text": ""
        })
    
    # Compile the final structured output.
    structured_output = {
        "content": content_sections,
        "resources": resources
    }
    
    return structured_output

# =============================================================================
# Helper: Get PDF total page count using PyMuPDF
def get_pdf_page_count(paper):
    try:
        doc = fitz.open(f"papers/{paper}.pdf")
        return len(doc)
    except Exception:
        return 1

# =============================================================================
# Helper: Convert compiled text (with block links in the format "[block:{n}]")
# into clickable Dash elements.
def parse_block_links(text, block_pages):
    parts = re.split(r'(\[block:\d+\])', text)
    children = []
    for part in parts:
        m = re.match(r'\[block:(\d+)\]', part)
        if m:
            block_idx = int(m.group(1))
            children.append(
                html.Span(
                    part,
                    id={'type': 'block-link', 'index': block_idx},
                    style={'color': 'blue', 'cursor': 'pointer', 'textDecoration': 'underline'}
                )
            )
        else:
            children.append(html.Span(part))
    return children

# =============================================================================
# Helper: Build nested accordions for the compiled structure.
def build_nested_compiled_structure_html(compiled_structure, block_pages):
    # Group content items by section.
    sections_dict = {}
    for item in compiled_structure.get("content", []):
        section = item.get("section", "section_1")
        sections_dict.setdefault(section, []).append(item)

    outer_items = []
    for section, items in sections_dict.items():
        # Use the first occurrence's section_block for the section header.
        section_block = items[0].get("section_block", "")
        section_link_props = {}
        if section_block:
            try:
                section_index = int(re.findall(r'\d+', str(section_block))[0])
                section_link_props = {
                    'id': {'type': 'block-link', 'index': section_index},
                    'style': {'color': 'blue', 'cursor': 'pointer'}
                }
            except Exception:
                section_link_props = {}
        inner_items = []
        for sub_item in items:
            subsection = sub_item.get("subsection", "subsection_1")
            subsection_block = sub_item.get("subsection_block", "")
            text = sub_item.get("text", "")
            subsection_link_props = {}
            if subsection_block:
                try:
                    subsection_index = int(re.findall(r'\d+', str(subsection_block))[0])
                    subsection_link_props = {
                        'id': {'type': 'block-link', 'index': subsection_index},
                        'style': {'color': 'blue', 'cursor': 'pointer'}
                    }
                except Exception:
                    subsection_link_props = {}
            inner_items.append(
                dbc.AccordionItem(
                    title=html.Span(subsection, **subsection_link_props),
                    children=parse_block_links(text, block_pages),
                    item_id=f"subitem-{subsection_block}"
                )
            )
        inner_accordion = dbc.Accordion(inner_items, flush=True, start_collapsed=True)
        outer_items.append(
            dbc.AccordionItem(
                title=html.Span(section, **section_link_props),
                children=inner_accordion,
                item_id=f"outeritem-{section_block}"
            )
        )

    compiled_content_div = dbc.Accordion(outer_items, flush=True, start_collapsed=True)

    # Build separate accordion for resources.
    resource_items = []
    for res in compiled_structure.get("resources", []):
        resource_ref = res.get("reference", "")
        content_val = res.get("content", "")
        description = res.get("description", "")
        content_block = res.get("content_block", "")
        resource_link_props = {}
        if content_block:
            try:
                if isinstance(content_block, int):
                    block_idx = content_block
                else:
                    block_idx = int(content_block.strip("[block:").strip("]"))
            except Exception:
                block_idx = 0
            resource_link_props = {
                'id': {'type': 'block-link', 'index': block_idx},
                'style': {'color': 'blue', 'cursor': 'pointer'}
            }
        resource_items.append(
            dbc.AccordionItem(
                title=html.Span(f"Resource {resource_ref}", **resource_link_props),
                children=[
                    html.P("Content: " + content_val),
                    html.P("Description: " + description)
                ],
                item_id=f"resource-{content_block}"
            )
        )
    compiled_resources_div = dbc.Accordion(resource_items, flush=True, start_collapsed=True)

    return html.Div([
        html.H4("Compiled Structure"),
        compiled_content_div,
        html.H4("Resources"),
        compiled_resources_div
    ])

# =============================================================================
# Build the Dash app layout
app = dash.Dash(__name__, external_stylesheets=[dbc.themes.BOOTSTRAP])
server = app.server

app.layout = html.Div([
    # Navbar: Title, paper dropdown, and page navigator (placed next to the title)
    dbc.NavbarSimple(
         brand="PDF Structure Compiler Visualizer",
         children=[
             dbc.ButtonGroup([
                 dbc.Button("<< First", id="first-page-btn", n_clicks=0, className="me-1"),
                 dbc.Button("< Prev Page", id="prev-page-btn", n_clicks=0, className="me-1"),
                 html.Span(
                     id="page-info",
                     children="Page ? of ?",
                     style={
                         'display': 'inline-block',
                         'verticalAlign': 'middle',
                         'marginTop': '7px',
                         'fontSize': '16px',
                         'color': 'white'
                     }
                 ),
                 dbc.Button("Next Page >", id="next-page-btn", n_clicks=0, className="me-1"),
                 dbc.Button("Last Page >>", id="last-page-btn", n_clicks=0)
             ], className="ms-3", style={'marginRight': '150px'}),
            dcc.Dropdown(
                id="paper-dropdown",
                options=[{"label": p, "value": p} for p in available_papers],
                placeholder="Select a paper",
                style={'width': '300px', 'marginRight': '50px'}
            )
         ],
         color="primary",
         dark=True,
         fluid=True,
         className="mb-4"
    ),
    
    dbc.Container([
         dbc.Row([
             dbc.Col([
                # Left Column: Display the PDF page content.
                html.Div(
                    id="page-content",
                    style={
                        'border': '1px solid #ddd',
                        'padding': '10px',
                        'min-height': '500px',
                        # 'max-height': '800px',
                        'overflow': 'auto',
                        'display': 'flex',
                        'justify-content': 'center',
                        'align-items': 'center'
                    }
                )
             ], width=8, style={'border-right': '1px solid #ddd', 'padding': '10px'}),
             dbc.Col([
                # Right Column: Display the compiled structure visualization.
                dbc.Card([
                    dbc.CardHeader("Compiled Structure", style={'background-color': '#f8f9fa'}),
                    dbc.CardBody([
                        html.Div(id="compiled-structure-div")
                    ])
                ], className="mb-3")
             ], width=4, style={'padding': '20px'})
         ])
    ], fluid=True),
    
    # Hidden dcc.Store components for sharing state.
    dcc.Store(id="selected-paper"),
    dcc.Store(id="current-page", data=1),
    dcc.Store(id="compiled-structure-store"),
    dcc.Store(id="block-pages-store"),  # Mapping: block index (as string) → page number.
    dcc.Store(id="total-pages", data=1)
])

# =============================================================================
# In the load_paper function - Fix block_pages creation:
@app.callback(
    [Output("selected-paper", "data"),
     Output("compiled-structure-store", "data"),
     Output("block-pages-store", "data"),
     Output("total-pages", "data"),
     Output("current-page", "data"),
     Output("compiled-structure-div", "children")],
    Input("paper-dropdown", "value")
)
def load_paper(paper):
    if paper is None:
        return dash.no_update, dash.no_update, dash.no_update, dash.no_update, dash.no_update, ""
    parsed_content = segmentation_results.get(paper, [])
    mapping = columns_mapping.get(paper, {})
    compiled_structure = compile_parsed_content(parsed_content, mapping)
    
    # Fix 1: Create block_pages using actual block numbers from content
    block_pages = {}
    for i, block in enumerate(parsed_content):
        block_pages[i] = block.get("page_number", 1)
    
    total_pages = get_pdf_page_count(paper)
    compiled_html = build_nested_compiled_structure_html(compiled_structure, block_pages)
    return paper, compiled_structure, block_pages, total_pages, 1, compiled_html

# In the update_page callback - Fix index handling:
@app.callback(
    [Output("current-page", "data", allow_duplicate=True),
     Output("page-info", "children", allow_duplicate=True)],
    [Input("first-page-btn", "n_clicks"),
     Input("prev-page-btn", "n_clicks"),
     Input("next-page-btn", "n_clicks"),
     Input("last-page-btn", "n_clicks"),
     Input({'type': 'block-link', 'index': ALL}, "n_clicks")],  # Changed to n_clicks
    [State("current-page", "data"),
     State("total-pages", "data"),
     State("selected-paper", "data"),
     State("block-pages-store", "data")],
    prevent_initial_call=True
)
def update_page(n_first, n_prev, n_next, n_last, block_clicks,
                current_page, total_pages, paper, block_pages):
    ctx = callback_context
    if not ctx.triggered or paper is None:
        raise dash.exceptions.PreventUpdate
    print(f'block_pages: {block_pages}')
    # Get the ID of the triggered component
    triggered_id = ctx.triggered[0]['prop_id']
    
    # Initialize new_page with current value
    new_page = current_page
    
    # Handle navigation buttons
    if "first-page-btn" in triggered_id:
        new_page = 1
    elif "prev-page-btn" in triggered_id:
        new_page = max(1, current_page - 1)
    elif "next-page-btn" in triggered_id:
        new_page = min(total_pages, current_page + 1)
    elif "last-page-btn" in triggered_id:
        new_page = total_pages
    elif "block-link" in triggered_id:
        # Fix 2: Get actual block index from component ID
        block_index = (
            ctx.triggered[0]['prop_id']
            .split('"index":')[1].split("}")[0].strip()
            .split(',')[0]
        )
        print(f'block_index: {block_index}')
        # print(f'block_pages[str(block_index)]: {block_pages[str(block_index)]}')
        new_page = block_pages.get(str(int(block_index)), current_page)
        print(f'new_page: {new_page}')

    page_info = f"Page {new_page} of {total_pages}"
    return new_page, page_info

# =============================================================================
# Callback 3: Render the PDF page content as an image.
@app.callback(
    Output('page-content', 'children'),
    [Input('current-page', 'data')],
    [State('selected-paper', 'data')]
)
def display_page_content(page_num, paper):
    if page_num is not None and paper:
        file_path = f"papers/{paper}.pdf"
        doc = fitz.open(file_path)
        # PyMuPDF uses 0-indexed pages; adjust accordingly.
        page = doc.load_page(page_num - 1)
        pix = page.get_pixmap(dpi=300)
        img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
        buffer = BytesIO()
        img.save(buffer, format="PNG")
        img_str = base64.b64encode(buffer.getvalue()).decode()
        img_data = f"data:image/png;base64,{img_str}"
        return html.Div([
            html.Img(
                src=img_data,
                style={
                    'max-width': '100%',
                    'max-height': '100%',
                    'height': 'auto',
                    'object-fit': 'contain'
                }
            )
        ])
    return ""

# =============================================================================
# Run the app
if __name__ == "__main__":
    app.run_server(debug=True, host='0.0.0.0', port=port)

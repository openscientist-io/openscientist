# Implementation Plan: Make File Upload Optional

**Issue:** #13 - File upload should be supported but not required
**Created:** 2024-12-04
**Complexity:** Medium (4-5 files, ~100 lines changed)

## Overview

Currently, OpenScientist requires users to upload at least one data file to create a discovery job. This requirement prevents users from running purely literature-based or computational investigations that don't need input data.

This plan outlines the changes needed to make file uploads optional while maintaining backward compatibility for jobs that do include data files.

## Goals

1. ✅ Allow job creation without any uploaded files
2. ✅ Maintain backward compatibility - existing data-file workflows still work
3. ✅ MCP server handles "no data" case gracefully
4. ✅ UI clearly indicates files are optional
5. ✅ Agent receives appropriate context when no files are provided

## Non-Goals

- ❌ Changing how data files are processed when present
- ❌ Modifying the knowledge graph schema
- ❌ Adding new job types or modes

## Files to Modify

1. `src/openscientist/web_app.py` - Remove validation, update UI labels
2. `src/openscientist/orchestrator.py` - Handle empty data_files list
3. `src/openscientist/mcp_server/server.py` - Make --data-file optional
4. `src/openscientist/job_manager.py` - Allow empty data_files in validation

## Detailed Implementation Steps

### Step 1: Update Web UI (`src/openscientist/web_app.py`)

**Location:** Lines 118-121, 197-202

**Changes:**

1. **Remove file requirement validation** (lines 118-121):
   ```python
   # DELETE these lines:
   # Check if files were uploaded
   if not _uploaded_files.get(session_id):
       ui.notify("Please upload at least one data file", type="negative")
       return
   ```

2. **Update upload field label** (line 197):
   ```python
   # CHANGE from:
   upload = ui.upload(
       label="Upload Data Files (Tabular, Structures, Sequences, Images)",

   # TO:
   upload = ui.upload(
       label="Upload Data Files (Optional - Tabular, Structures, Sequences, Images)",
   ```

3. **Handle empty file list** (lines 127-134):
   ```python
   # CHANGE:
   # Save uploaded files to temp location
   data_files = []
   for uploaded_file in _uploaded_files[session_id]:
       # ... existing code ...

   # TO:
   # Save uploaded files to temp location
   data_files = []
   if _uploaded_files.get(session_id):  # Only if files were uploaded
       for uploaded_file in _uploaded_files[session_id]:
           # ... existing code ...
   ```

**Verification:**
- UI shows "Upload Data Files (Optional - ...)"
- Can click "Start Discovery" without uploading files
- No error notification when submitting without files

---

### Step 2: Update Job Creation (`src/openscientist/orchestrator.py`)

**Location:** Lines 83-93, 106-116, 156

**Changes:**

1. **Make file copying conditional** (lines 83-93):
   ```python
   # CHANGE from:
   # Copy data files to job directory, preserving original names/extensions
   data_paths = []
   for data_file in data_files:
       original_name = Path(data_file).name
       dest = job_dir / "data" / original_name
       import shutil
       shutil.copy(data_file, dest)
       data_paths.append(dest)

   # TO:
   # Copy data files to job directory, preserving original names/extensions
   data_paths = []
   if data_files:  # Only copy if files were provided
       for data_file in data_files:
           original_name = Path(data_file).name
           dest = job_dir / "data" / original_name
           import shutil
           shutil.copy(data_file, dest)
           data_paths.append(dest)
   ```

2. **Handle empty data_paths for KG initialization** (lines 106-116):
   ```python
   # CHANGE from:
   first_file = data_paths[0]
   file_info = get_file_info(first_file)
   kg.set_data_summary({
       "files": [str(p.name) for p in data_paths],
       "file_type": file_info["file_type"],
       "file_size_mb": file_info["size"] / (1024 * 1024)
   })

   # TO:
   if data_paths:
       first_file = data_paths[0]
       file_info = get_file_info(first_file)
       kg.set_data_summary({
           "files": [str(p.name) for p in data_paths],
           "file_type": file_info["file_type"],
           "file_size_mb": file_info["size"] / (1024 * 1024)
       })
   else:
       # No data files provided
       kg.set_data_summary({
           "files": [],
           "file_type": "none",
           "file_size_mb": 0
       })
   ```

3. **Handle empty data_files in discovery** (line 156):
   ```python
   # CHANGE from:
   data_file = Path(config["data_files"][0])  # Use first data file

   # TO:
   data_file = Path(config["data_files"][0]) if config["data_files"] else None
   ```

4. **Make MCP data-file argument conditional** (lines 177-178):
   ```python
   # CHANGE from:
   mcp_config = {
       "mcpServers": {
           "openscientist-tools": {
               "command": "python",
               "args": [
                   "-m", "openscientist.mcp_server",
                   "--job-dir", str(job_dir.absolute()),
                   "--data-file", str(data_file.absolute())
               ],

   # TO:
   mcp_args = [
       "-m", "openscientist.mcp_server",
       "--job-dir", str(job_dir.absolute()),
   ]
   if data_file:
       mcp_args.extend(["--data-file", str(data_file.absolute())])

   mcp_config = {
       "mcpServers": {
           "openscientist-tools": {
               "command": "python",
               "args": mcp_args,
   ```

5. **Update initial prompt to handle no data** (lines 208-210):
   ```python
   # CHANGE:
   Data summary:
   - Files: {config['data_files']}
   - Columns: {kg.data['data_summary'].get('columns', [])}
   - Samples: {kg.data['data_summary'].get('n_samples', 'Unknown')}

   # TO:
   {f'''Data summary:
   - Files: {config['data_files']}
   - Columns: {kg.data['data_summary'].get('columns', [])}
   - Samples: {kg.data['data_summary'].get('n_samples', 'Unknown')}
   ''' if config['data_files'] else 'No data files provided. You may use literature search and computational methods.'}
   ```

**Verification:**
- Job can be created with empty data_files list
- MCP config doesn't include --data-file when no files
- Knowledge graph has proper "no data" summary

---

### Step 3: Update MCP Server (`src/openscientist/mcp_server/server.py`)

**Location:** Lines 229, 238-248, 42-88

**Changes:**

1. **Make --data-file argument optional** (line 229):
   ```python
   # CHANGE from:
   parser.add_argument("--data-file", required=True, help="Primary data file")

   # TO:
   parser.add_argument("--data-file", required=False, default=None, help="Primary data file (optional)")
   ```

2. **Handle None data file** (lines 238-248):
   ```python
   # CHANGE from:
   # Save primary data file path for lazy loading
   primary_file = Path(args.data_file)
   DATA_FILE_PATH = primary_file

   # Get metadata only (fast operation - no actual data loading)
   try:
       primary_info = get_file_info(primary_file)
       DATA_FILES = [primary_info]
   except Exception as e:
       print(f"❌ ERROR: Could not read file info for {primary_file}: {e}", file=sys.stderr)
       sys.exit(1)

   # TO:
   # Save primary data file path for lazy loading (if provided)
   if args.data_file:
       primary_file = Path(args.data_file)
       DATA_FILE_PATH = primary_file

       # Get metadata only (fast operation - no actual data loading)
       try:
           primary_info = get_file_info(primary_file)
           DATA_FILES = [primary_info]
       except Exception as e:
           print(f"❌ ERROR: Could not read file info for {primary_file}: {e}", file=sys.stderr)
           sys.exit(1)
   else:
       # No primary data file
       DATA_FILE_PATH = None
       DATA_FILES = []
       print(f"ℹ️  No data file provided - server running in no-data mode", file=sys.stderr)
   ```

3. **Update file info logging** (lines 251-252):
   ```python
   # CHANGE from:
   file_size_mb = primary_info['size'] / (1024 * 1024)
   print(f"📂 Data file registered: {primary_file.name} ({file_size_mb:.1f} MB) - will load on first use", file=sys.stderr)

   # TO:
   if primary_info:
       file_size_mb = primary_info['size'] / (1024 * 1024)
       print(f"📂 Data file registered: {primary_file.name} ({file_size_mb:.1f} MB) - will load on first use", file=sys.stderr)
   ```

4. **Update ensure_data_loaded for no-data case** (lines 58-60):
   ```python
   # CHANGE from:
   # No file path? Server not initialized properly
   if DATA_FILE_PATH is None:
       DATA_LOAD_ERROR = "MCP server not initialized with data file path"
       return DATA_LOAD_ERROR

   # TO:
   # No file path means no data was provided (valid case)
   if DATA_FILE_PATH is None:
       DATA_LOAD_ERROR = None  # Not an error, just no data
       DATA = None
       return None  # Success - no data to load
   ```

5. **Update execute_code to clarify no-data message** (lines 115-117):
   ```python
   # CHANGE from:
   load_error = ensure_data_loaded()
   if load_error is not None:
       return f"❌ ERROR: Cannot execute code - data file failed to load.\n\n{load_error}"

   # TO:
   load_error = ensure_data_loaded()
   if load_error is not None:
       return f"❌ ERROR: Cannot execute code - data file failed to load.\n\n{load_error}"

   # Inform agent if no data file was provided
   if DATA is None and DATA_FILE_PATH is None:
       # Note: This is informational, not an error. Code can still run.
       pass  # Agent will see data=None in their namespace
   ```

**Verification:**
- MCP server starts without --data-file argument
- No error when DATA_FILE_PATH is None
- execute_code works with data=None
- data_files list is empty when no files

---

### Step 4: Update Job Manager Validation (`src/openscientist/job_manager.py`)

**Location:** Lines 96-97

**Changes:**

1. **Allow empty data_files list**:
   ```python
   # CHANGE from:
   def create_job(
       self,
       job_id: str,
       research_question: str,
       data_files: List[Path],  # Required, non-empty

   # TO:
   def create_job(
       self,
       job_id: str,
       research_question: str,
       data_files: List[Path] = None,  # Optional, can be empty
   ```

2. **Handle None data_files**:
   ```python
   # ADD after line 117 (before create_job call):
   # Handle None or empty data_files
   if data_files is None:
       data_files = []
   ```

**Verification:**
- job_manager.create_job() accepts empty list
- No crashes when data_files=[]

---

## Testing Plan

### Manual Testing

1. **No Files - Literature Only**
   - Submit job with research question "What are the known mechanisms of ferroptosis?" without uploading files
   - Verify job creates successfully
   - Verify agent can use search_pubmed
   - Verify execute_code runs (with data=None)

2. **With Files - Normal Flow**
   - Submit job with CSV file as before
   - Verify no regression in existing behavior
   - Verify data loads correctly

3. **Mixed Scenarios**
   - Create jobs alternating between with-files and without-files
   - Verify no state leakage between jobs

### Automated Testing

Create test case in `tests/test_optional_files.py`:

```python
def test_job_creation_without_files():
    """Test creating a job without any data files."""
    manager = JobManager()

    job_info = manager.create_job(
        job_id="test_no_files",
        research_question="Test question",
        data_files=[],  # Empty list
        max_iterations=2
    )

    assert job_info.job_id == "test_no_files"

    # Check config
    config_path = manager.jobs_dir / "test_no_files" / "config.json"
    with open(config_path) as f:
        config = json.load(f)

    assert config["data_files"] == []

    # Check knowledge graph
    kg_path = manager.jobs_dir / "test_no_files" / "knowledge_graph.json"
    kg = KnowledgeGraph.load(kg_path)

    assert kg.data["data_summary"]["files"] == []
    assert kg.data["data_summary"]["file_type"] == "none"
```

## Edge Cases to Handle

1. **Empty string in data_files list**: `data_files = [""]`
   - Solution: Filter out empty strings before processing

2. **Invalid file paths**: `data_files = [Path("/nonexistent")]`
   - Solution: Existing validation in file_loader.py handles this

3. **Agent tries to use data when None**:
   - Solution: Execute code already handles data=None gracefully
   - Agent will see `data is None` and can check before using

## Migration Path

This change is **backward compatible** - no migration needed:

- Existing jobs with files: Work exactly as before
- New jobs without files: New capability, opt-in

## Success Criteria

- ✅ Can create job without uploading files
- ✅ MCP server starts successfully with no --data-file
- ✅ execute_code works (returns data=None to agent)
- ✅ search_pubmed works independently
- ✅ Existing file-based workflows unchanged
- ✅ UI clearly shows files are optional
- ✅ No crashes or errors in no-data mode

## Rollback Plan

If issues arise:
1. Revert all changes (single PR, easy to revert)
2. Add back validation in web_app.py line 118
3. Restore required=True for --data-file argument

No database migrations or data changes needed.

## Implementation Time Estimate

- Step 1 (Web UI): 15 minutes
- Step 2 (Orchestrator): 30 minutes
- Step 3 (MCP Server): 30 minutes
- Step 4 (Job Manager): 10 minutes
- Testing: 30 minutes
- Documentation updates: 15 minutes

**Total: ~2.5 hours**

## Follow-up Work (Future)

These are **not required** for this change, but could be nice enhancements:

1. Add example research questions for no-data mode in UI
2. Create skills specifically for literature-only investigations
3. Add telemetry to track usage of no-data vs data-based jobs
4. Update documentation with no-data use cases

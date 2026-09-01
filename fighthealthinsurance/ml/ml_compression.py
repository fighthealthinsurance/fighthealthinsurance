import re
from pathlib import Path
from llmlingua import PromptCompressor
from typing import Any, cast

class MedicalContextCompressor:
    def __init__(self, device: str = "cpu"):
        """
        Initializes the LLMLingua-2 compressor.
        """
        # Path building
        self.FHI_Parent = Path(__file__).resolve().parent.parent.parent.parent
        # Assumes the model folder is sitting next to the very top FHI folder
        self.local_model_path = str(self.FHI_Parent / "llmlingua-2-model")
        
        # Initialize the compressor
        self.compressor = PromptCompressor(
            model_name=self.local_model_path, # Use local path instead of HF hub string
            use_llmlingua2=True,
            # device options are cpu, mps (Apple Silicon), or cuda.  The cuda options will 
            # work with both Nvidia GPUs and AMD GPUs that have ROCm enabled. 
            device_map=device,
        )

        # Rename model name after compressor instantiation because of local model files
        # Not necessary if self.compressor uses HF hub instead
        self.compressor.model_name = "microsoft/llmlingua-2-bert-base-multilingual-cased"

        # Line breaks won't be compressed to help maintain paragraphs for logical separation
        self.base_force_tokens = ['\n']

    def _extract_medical_tokens(self, text: str) -> list[str]:
        """
        Scans the text for medical terms (CPT codes, ICD codes, units, dosages) 
        as well as dollar amounts, dates, study data, etc and returns them as a list 
        of exact strings to protect.
        """
        protected_tokens: set[str] = set()
        
        # Billing codes
        # Label prefixes (e.g., ICD-10, CPT, HCPCS, etc.)
        prefix_pattern = r'\b(?:ICD-(?:10|9)(?:-[A-Z]+)?|CPT|HCPCS)\b'
        protected_tokens.update(re.findall(prefix_pattern, text, flags=re.IGNORECASE))
        # CPT/HCPCS values (e.g., 95810, J0490)
        protected_tokens.update(re.findall(r'\b[A-Z0-9]{5}\b', text))
        # ICD values (e.g., M54.5, Z01.419)
        protected_tokens.update(re.findall(r'\b[A-Z]\d{2}(?:\.\d{1,4})?\b', text))
        
        # Medication doses, preserving concentrations and unit amts (mg, ml, kg, etc.)
        unit_pattern = r'\b\d+(?:\.\d+)?(?:\s*-\s*\d+(?:\.\d+)?)?\s*(?:mg|ml|mcg|kg|g|cc|mmol|iu|units?)\b'
        unit_matches = re.findall(unit_pattern, text, flags=re.IGNORECASE)
        protected_tokens.update([u.strip() for u in unit_matches])

        # Billing/Dollar Amounts (e.g., $1,349.88) and percentages, such as copays (e.g., 15%)
        protected_tokens.update(re.findall(r'\$\d{1,3}(?:,\d{3})*(?:\.\d{2})?\b|\b\d+(?:\.\d+)?%', text))

        # 10-digit numbers to protect NPIs and potentially Claim ID numbers
        protected_tokens.update(re.findall(r'\b\d{10}\b', text))

        # RxNav/RxNorm/RxCUI and National Drug Codes (NDCs)
        protected_tokens.update(re.findall(r'\b\d{4,5}-\d{3,4}-\d{1,2}\b', text))
        protected_tokens.update(re.findall(r'\bRxCUI:?\s*\d+\b', text, flags=re.IGNORECASE))

        # Clinical trial identifiers
        protected_tokens.update(re.findall(r'\bNCT\d{8}\b', text, flags=re.IGNORECASE))

        # PubMed identifiers and DOIs
        protected_tokens.update(re.findall(r'\bPMID:?\s*\d+\b', text, flags=re.IGNORECASE))
        protected_tokens.update(re.findall(r'\b10\.\d{4,9}/[-._;()/:A-Za-z0-9]+\b', text))

        # Preserve dates
        protected_tokens.update(re.findall(r'\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}/\d{1,2}/\d{2,4}\b', text))

        return list(protected_tokens)

    def compress_context(self, context: str, max_allowed_tokens: int) -> dict[str, Any]:
        """
        Compresses the medical or policy context to fit within a specific token budget
        while protecting essential formatting and medical codes.
        """
        # Dynamically build the list of tokens that cannot be deleted based on above rules
        medical_tokens = self._extract_medical_tokens(context)
        combined_force_tokens = self.base_force_tokens + medical_tokens

        # Compress targeting a specific token budget (max_allowed_tokens).  Can
        # instead be set to target a specific compression ratio, if desired. 
        compression_result = cast(dict[str, Any], self.compressor.compress_prompt(
            context=[context],
            target_token=max_allowed_tokens, # Fit the defined context window
            force_tokens=combined_force_tokens,
            drop_consecutive=True
            ),
        )

        return {
            "compressed_text": compression_result["compressed_prompt"],
            "original_tokens": compression_result["origin_tokens"],
            "compressed_tokens": compression_result["compressed_tokens"],
            "ratio": compression_result["ratio"]
        }

if __name__ == "__main__":
    # Integration test.
    # 1. Sample medical text containing billing codes, medication names/doses, costs
    sample_text = """
    PATIENT HISTORY & CLINICAL SUMMARY:
    Patient presents today for evaluation of chronic low back pain (ICD-10 code M54.5).
    Administered 5mg of Cyclobenzaprine and 100ml of saline solution.
    Procedures performed during this visit include CPT 95810 for monitoring.
    Please ensure follow-up occurs within 2 weeks if symptoms worsen or do not improve.
    Total cost estimated at $450.00 with a 10% insurance co-pay.
    """

    print("--- Initializing MedicalContextCompressor ---")
    # Initialize using 'cpu' (can change to cuda if applicable)
    compressor = MedicalContextCompressor(device="cpu")

    print("\n--- Testing Token Extraction ---")
    extracted_tokens = compressor._extract_medical_tokens(sample_text)
    print(f"Extracted Medical Tokens: {extracted_tokens}")

    print("\n--- Testing Context Compression ---")
    # Set a target budget (e.g., compress down to 50 tokens)
    result = compressor.compress_context(context=sample_text, max_allowed_tokens=45)

    print("\n--- Compression Results ---")
    print(f"Original Token Count : {result['original_tokens']}")
    print(f"Compressed Token Count: {result['compressed_tokens']}")
    print(f"Compression Ratio     : {result['ratio']}")
    print("\nCompressed Output Text:")
    print(result["compressed_text"])
import yaml
import os

class NucleiTemplateGenerator:
    def __init__(self, output_dir="bounty_missions/tools/ntg/templates"):
        self.output_dir = output_dir
        if not os.path.exists(self.output_dir):
            os.makedirs(self.output_dir)

    def generate_skeleton(self, cve_id, severity, description):
        """Generates a standard Nuclei YAML skeleton."""
        template = {
            "id": cve_id.lower(),
            "info": {
                "name": f"{cve_id} Detection",
                "author": "gemini-cli-hunter",
                "severity": severity.lower(),
                "description": description,
                "classification": {
                    "cve-id": cve_id
                },
                "tags": "cve,bounty"
            },
            "http": [
                {
                    "method": "GET",
                    "path": ["{{BaseURL}}/path-to-exploit"],
                    "matchers-condition": "and",
                    "matchers": [
                        {
                            "type": "word",
                            "words": ["vulnerable-string"],
                            "part": "body"
                        },
                        {
                            "type": "status",
                            "status": [200]
                        }
                    ]
                }
            ]
        }
        
        file_path = os.path.join(self.output_dir, f"{cve_id}.yaml")
        with open(file_path, 'w') as f:
            yaml.dump(template, f, sort_keys=False)
        
        return file_path

if __name__ == "__main__":
    generator = NucleiTemplateGenerator()
    # Example usage:
    # path = generator.generate_skeleton("CVE-2024-XXXX", "High", "Test description")
    # print(f"Template generated at: {path}")

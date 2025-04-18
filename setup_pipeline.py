"""
Script to set up the Azure ML pipeline and all required resources.
Run this from your development environment after setting up the directory structure.
"""
import os
import shutil
import argparse
from azure.ai.ml import MLClient, command, dsl, Input, Output
from azure.ai.ml.entities import Environment, BuildContext, AmlCompute
from azure.identity import DefaultAzureCredential
from azure.mgmt.authorization import AuthorizationManagementClient
from azure.mgmt.authorization.models import RoleAssignmentCreateParameters
from azure.core.exceptions import ResourceExistsError

import uuid
from dotenv import load_dotenv
# Load environment variables from .env file
load_dotenv()

def main():
    # Parse command line arguments
    parser = argparse.ArgumentParser(description='Setup and optionally run an Azure ML pipeline')
    parser.add_argument('--run', action='store_true', help='Run the pipeline after setup')
    args = parser.parse_args()

    # Connect to your AML workspace
    ml_client = MLClient(
        DefaultAzureCredential(), 
        subscription_id=os.environ["subscription_id"],  # Replace with your subscription ID
        resource_group_name=os.environ["resource_group_name"],  # Replace with your resource group
        workspace_name=os.environ["workspace_name"]  # Replace with your workspace name
    )
    
    # Set up directory structure
    directories = [
        "environments/pytorch",
        "environments/onnx2c",
        "environments/gcc",
        "src/pytorch_train",
        "src/onnx2c",
        "src/compile_test",
        "src/minimal_binary"
    ]
    missing_directories = [directory for directory in directories if not os.path.exists(directory)]
    
    if missing_directories:
        print("The following required directories are missing:")
        for directory in missing_directories:
            print(f" - {directory}")
        raise FileNotFoundError("One or more required directories are missing. Please create them and try again.")
    else:
        print("All required directories are present.")
    
    
    # Create compute resources
    print("Creating compute cluster...")
    if "cpu-cluster" not in [c.name for c in ml_client.compute.list()]:
        compute_cluster = AmlCompute(
            name="cpu-cluster",
            type="amlcompute",
            size="Standard_DS3_v2",
            min_instances=0,
            max_instances=4,
            idle_time_before_scale_down=120,
            identity={"type": "SystemAssigned"}  # Enable system-managed identity
        )
        ml_client.compute.begin_create_or_update(compute_cluster).result()
        print("Compute cluster created.")
    else:
        print("Compute cluster already exists.")
    #Get the system assigned identity of the compute cluster
    principal_id = ml_client.compute.get("cpu-cluster").identity.principal_id
    print(f"Compute cluster identity principal ID: {principal_id}")
    storage_account_name = ml_client.datastores.get("workspaceblobstore").account_name
    
    # Assign Storage Blob Data Contributor role to the compute cluster identity for the storage store
    
    # Initialize credentials and clients
    credential = DefaultAzureCredential()
    authorization_client = AuthorizationManagementClient(credential, os.environ["subscription_id"])

    # Define parameters
    scope = f"/subscriptions/{os.environ["subscription_id"]}/resourceGroups/{os.environ["resource_group_name"]}/providers/Microsoft.Storage/storageAccounts/{storage_account_name}"  # Replace with your resource scope
    role_definition_id = f"/subscriptions/{os.environ["subscription_id"]}/providers/Microsoft.Authorization/roleDefinitions/ba92f5b4-2d11-453d-a403-e96b0029c9fe"  # Replace with the role definition ID (e.g., "Storage Blob Data Contributor")
    

    # Create a role assignment
    role_assignment_id = str(uuid.uuid4())  # Generate a unique ID for the role assignment
    role_assignment_params = RoleAssignmentCreateParameters(
        role_definition_id=role_definition_id,
        principal_id= principal_id,
        principal_type="ServicePrincipal"  # Explicitly set the PrincipalType

    )
    try:
        role_assignment = authorization_client.role_assignments.create(
            scope=scope,
            role_assignment_name=role_assignment_id,
            parameters=role_assignment_params
        )

        print(f"Role assignment created: {role_assignment.id}")
    except ResourceExistsError as e:
        print("Role assignment already exists. Skipping creation.")
    
    
    
    
    # ml_client.datastores.get("workspaceblobstore").assign_role(
    #     role_definition_name="Storage Blob Data Contributor",
    #     principal_id=compute_cluster.identity.principal_id,  # System assigned identity
    #     scope=ml_client.datastores.get("workspaceblobstore").id  # Scope of the role assignment
    # )
    # print("Assigned Storage Blob Data Contributor role to compute cluster identity.")
    
    
    # Create environments
    print("Creating environments...")
    env_configs = [
        ("pytorch-onnx-env", "environments/pytorch", "Environment for PyTorch training and ONNX export"),
        ("onnx2c-env", "environments/onnx2c", "Environment for ONNX to C conversion"),
        ("gcc-env", "environments/gcc", "Environment for C compilation and testing")
    ]
    
    # Dictionary to store the latest environment versions
    latest_envs = {}
    
    for env_name, env_dir, env_description in env_configs:
        # Create or update the environment
        env = Environment(
            name=env_name,
            description=env_description,
            build=BuildContext(path=env_dir)
        )
        registered_env = ml_client.environments.create_or_update(env)
        print(f"Environment {env_name} created/updated with version {registered_env.version}")
        
        # Store the latest version
        latest_envs[env_name] = f"{env_name}:{registered_env.version}"
    
    # Define components
    print("Creating pipeline components...")
    
    # 1. PyTorch Training Component
    train_pytorch_model = command(
        name="pytorch_train",
        display_name="Train PyTorch Model and Export to ONNX",
        description="Trains a PyTorch model and exports it to ONNX format",
        environment=latest_envs["pytorch-onnx-env"],
        compute="cpu-cluster",
        code="./src/pytorch_train",
        outputs=dict(
            output_dir=Output(type="uri_folder", description="Output directory for model and test data")
        ),
        command="python run.py --output_dir ${{outputs.output_dir}}"
    )
    
    # 2. ONNX to C Conversion Component - Simplified to only produce core C model files
    convert_onnx_to_c = command(
        name="onnx2c",
        display_name="Convert ONNX to C",
        description="Converts ONNX model to C code using onnx2c",
        environment=latest_envs["onnx2c-env"],
        compute="cpu-cluster",
        code="./src/onnx2c",
        inputs=dict(
            model_dir=Input(type="uri_folder", description="Directory containing ONNX model and test data")
        ),
        outputs=dict(
            output_dir=Output(type="uri_folder", description="Output directory for core C model code")
        ),
        command="python run.py --model_dir ${{inputs.model_dir}} --output_dir ${{outputs.output_dir}}"
    )
    
    # 3. C Compilation and Testing Component - Now gets inputs from both training and ONNX2C
    compile_and_test = command(
        name="compile_and_test",
        display_name="Compile C Code and Run Tests",
        description="Compiles C code and runs tests",
        environment=latest_envs["gcc-env"],
        compute="cpu-cluster",
        code="./src/compile_test",
        inputs=dict(
            c_code_dir=Input(type="uri_folder", description="Directory containing core C model code"),
            model_dir=Input(type="uri_folder", description="Directory containing test data from model training")
        ),
        outputs=dict(
            output_dir=Output(type="uri_folder", description="Output directory for test results")
        ),
        command="python run.py --c_code_dir ${{inputs.c_code_dir}} --model_dir ${{inputs.model_dir}} --output_dir ${{outputs.output_dir}}"
    )
    
    # 4. Build Minimal Binary Component - Only depends on core C model code
    build_minimal_binary = command(
        name="build_minimal",
        display_name="Build Minimal Binary",
        description="Creates minimal binary for deployment",
        environment=latest_envs["gcc-env"],
        compute="cpu-cluster",
        code="./src/minimal_binary",
        inputs=dict(
            c_code_dir=Input(type="uri_folder", description="Directory containing core C model code")
        ),
        outputs=dict(
            output_dir=Output(type="uri_folder", description="Output directory for minimal binary")
        ),
        command="python run.py --c_code_dir ${{inputs.c_code_dir}} --output_dir ${{outputs.output_dir}}"
    )
    
    # Define the pipeline with optimized connections between components
    @dsl.pipeline(
        name="pytorch-onnx-c-pipeline",
        description="Pipeline for training PyTorch model, converting to ONNX, C, and building minimal binary",
        compute="cpu-cluster"
    )
    def nn_pipeline():
        # Train PyTorch model
        train_step = train_pytorch_model()
        
        # Convert ONNX to C - gets input from training step
        onnx2c_step = convert_onnx_to_c(model_dir=train_step.outputs.output_dir)
        
        # Compile and test C code - now gets inputs from both train_step and onnx2c_step
        compile_step = compile_and_test(
            c_code_dir=onnx2c_step.outputs.output_dir,
            model_dir=train_step.outputs.output_dir
        )
        
        # Build minimal binary - only depends on core C model code
        binary_step = build_minimal_binary(c_code_dir=onnx2c_step.outputs.output_dir)
        
        # Return all outputs
        return {
            "training_output": train_step.outputs.output_dir,
            "c_code_output": onnx2c_step.outputs.output_dir,
            "test_results": compile_step.outputs.output_dir,
            "minimal_binary": binary_step.outputs.output_dir
        }
    
    # Create pipeline
    pipeline = nn_pipeline()
        
    # Run the pipeline if the --run flag is provided
    if args.run:
        pipeline_job = ml_client.jobs.create_or_update(pipeline)
        print(f"Pipeline job submitted with ID: {pipeline_job.name}")
    else:
        print("\nSetup complete! Pipeline is ready to be submitted.")
        print("Run the pipeline with the --run flag to execute it.")

if __name__ == "__main__":
    main()
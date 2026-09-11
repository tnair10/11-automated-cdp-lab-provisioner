pipeline {
    agent any

    options {
        timestamps()
        disableConcurrentBuilds()
        timeout(time: 10, unit: 'MINUTES')
    }

    environment {
        P11_DIRECT_TERRAFORM = '1'
        TF_PLUGIN_CACHE_DIR = '/terraform-plugin-cache'
        TF_IN_AUTOMATION = '1'

        AWS_ACCESS_KEY_ID = 'test'
        AWS_SECRET_ACCESS_KEY = 'test'
        AWS_DEFAULT_REGION = 'us-east-1'
    }

    stages {
        stage('Preflight') {
            steps {
                dir('/workspace') {
                    sh '''
                        set -eu

                        echo "========================================"
                        echo " PROJECT 11 JENKINS PREFLIGHT"
                        echo "========================================"

                        echo
                        echo "=== Toolchain ==="
                        python --version
                        terraform version | head -2
                        docker --version
                        docker compose version

                        echo
                        echo "=== Cost guard ==="
                        python automation/validate_cost_guard.py

                        echo
                        echo "=== LocalStack identity ==="
                        python - <<'PY'
import boto3

client = boto3.client(
    "sts",
    endpoint_url="http://p11-localstack:4566",
    region_name="us-east-1",
    aws_access_key_id="test",
    aws_secret_access_key="test",
)

identity = client.get_caller_identity()

print("Account:", identity["Account"])

assert identity["Account"] == "000000000000"

print("LocalStack identity: PASS")
print("Real AWS account used: NO")
PY
                    '''
                }
            }
        }

        stage('Provision Lab') {
            steps {
                dir('/workspace') {
                    sh '''
                        set -eu

                        python labctl up \
                          --backend localstack \
                          --profile minimal \
                          --security none
                    '''
                }
            }
        }

        stage('Validate Lab') {
            steps {
                dir('/workspace') {
                    sh '''
                        set -eu

                        python labctl validate \
                          --backend localstack
                    '''
                }
            }
        }

        stage('Publish Evidence') {
            steps {
                sh '''
                    set -eu

                    mkdir -p "$WORKSPACE/evidence"

                    cp \
                      /workspace/results/localstack_lab_run.json \
                      "$WORKSPACE/evidence/localstack_lab_run.json"

                    echo
                    echo "=== Jenkins archived evidence ==="

                    python -m json.tool \
                      "$WORKSPACE/evidence/localstack_lab_run.json"
                '''

                archiveArtifacts(
                    artifacts: 'evidence/localstack_lab_run.json',
                    fingerprint: true
                )
            }
        }
    }

    post {
        success {
            echo '''
========================================
 PROJECT 11 JENKINS PIPELINE: PASS
 Backend: LocalStack
 Real AWS resources: NO
 AWS cost: $0
========================================
'''
        }

        failure {
            echo '''
========================================
 PROJECT 11 JENKINS PIPELINE: FAIL
========================================
'''
        }
    }
}

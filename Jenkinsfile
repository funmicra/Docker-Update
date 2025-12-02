pipeline {
    agent any
    triggers {
        githubPush()
    }


    environment {
        REPO_URL = 'https://github.com/funmicra/Docker-Update.git'
        BRANCH = 'master'
        COMPOSE_PROJECT_NAME = 'docker-update'
        REGISTRY_URL = "registry.black-crab.cc"
        IMAGE_NAME   = "docker-update"
        FULL_IMAGE   = "${env.REGISTRY_URL}/${env.IMAGE_NAME}:latest"
    }

    stages {
        // Stage to checkout code from GitHub repository
        stage('Checkout') {
            steps {
                echo "Checking out GitHub repository..."
                git branch: "${BRANCH}", url: "${REPO_URL}"
            }
        }

        // Stage to build Docker image
        stage('Build Docker Image') {
            steps {
                script {
                    sh """
                    docker build -t ${FULL_IMAGE} .
                    """
                }
            }
        }

        // Stage to authenticate to Nexus registry
        stage('Authenticate to Registry') {
            steps {
                withCredentials([usernamePassword(
                    credentialsId: 'nexus_registry_login',
                    usernameVariable: 'REG_USER',
                    passwordVariable: 'REG_PASS'
                )]) {
                    sh '''
                    echo "$REG_PASS" | docker login ${REGISTRY_URL} -u "$REG_USER" --password-stdin
                    '''
                }
            }
        }

        // Stage to push Docker image to Nexus registry
        stage('Push to Nexus Registry') {
            steps {
                sh """
                docker push ${FULL_IMAGE}
                """
            }
        }

        // Stage to deploy the updated Docker image to the remote host
        stage('Deploy to Remote Host') {
            steps {
                sshagent(['DEBIANSERVER']) {
                    sh """
                    ssh -o StrictHostKeyChecking=no funmicra@192.168.88.22 '
                    if docker compose -p ${COMPOSE_PROJECT_NAME} ps -q | grep -q .; then
                        docker compose -p ${COMPOSE_PROJECT_NAME} down
                    else
                        echo "No running containers to stop."
                    fi
                        cd /home/funmicra/stacks/docker-update &&
                        docker compose -p ${COMPOSE_PROJECT_NAME} pull &&
                        docker compose -p ${COMPOSE_PROJECT_NAME} up -d
                    '

                    """
                }
            }
        }
    }

    post {
        success {
            echo "Deployment completed successfully!"
        }
        failure {
            echo "Deployment failed. Check the logs."
        }
    }
}

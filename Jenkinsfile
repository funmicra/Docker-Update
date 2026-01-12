pipeline {
    agent any
    triggers {
        githubPush()
    }


    environment {
        REPO_URL = 'https://github.com/funmicra/Docker-Update.git'
        BRANCH = 'master'
        COMPOSE_PROJECT_NAME = 'docker-update'
        REGISTRY_URL = "docker.io/funmicra"
        IMAGE_NAME   = "docker-update"
        FULL_IMAGE   = "${env.REGISTRY_URL}/${env.IMAGE_NAME}:latest"
    }

    stages {
        stage('Checkout') {
            steps {
                echo "Checking out GitHub repository..."
                git branch: "${BRANCH}", url: "${REPO_URL}"
            }
        }

        stage('Build Docker Image') {
            steps {
                script {
                    sh """
                    docker build -t ${FULL_IMAGE} .
                    """
                }
            }
        }

        stage('Authenticate to Registry') {
            steps {
                withCredentials([usernamePassword(
                    credentialsId: 'DOCKER_HUB_CREDENTIALS',
                    usernameVariable: 'REG_USER',
                    passwordVariable: 'REG_PASS'
                )]) {
                    sh '''
                    echo "$REG_PASS" | docker login ${REGISTRY_URL} -u "$REG_USER" --password-stdin
                    '''
                }
            }
        }

        stage('Push to DockerHub') {
            steps {
                sh """
                docker push ${FULL_IMAGE}
                """
            }
        }

        stage('Deploy to Remote Host') {
            steps {
                sshagent(['DEBIANSERVER']) {
                    sh """
                    ssh -o StrictHostKeyChecking=no ansible@192.168.88.22 '
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

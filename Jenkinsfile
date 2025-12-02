pipeline {
    agent any


    environment {
        REPO_URL = 'https://github.com/funmicra/Docker-Update.git'
        BRANCH = 'master'
        COMPOSE_PROJECT_NAME = 'Docker-Update'
        DOCKERHUB_CREDENTIALS = 'DOCKER_HUB'
        DOCKERHUB_REPO = 'funmicra/docker-update'
    }

    stages {
        stage('Checkout') {
            steps {
                echo "Checking out GitHub repository..."
                git branch: "${BRANCH}", url: "${REPO_URL}"
            }
        }

        stage('Build Docker Images') {
            steps {
                echo "Building Docker images..."
                sh 'docker-compose -p ${COMPOSE_PROJECT_NAME} build'
            }
        }


        stage('Tag and Push to Docker Hub') {
            steps {
                withCredentials([usernamePassword(credentialsId: "${DOCKERHUB_CREDENTIALS}", usernameVariable: 'DOCKER_USER', passwordVariable: 'DOCKER_PASS')]) {
                    sh """
                    # Login to Docker Hub
                    echo "$DOCKER_PASS" | docker login -u "$DOCKER_USER" --password-stdin

                    # Tag all images defined in docker-compose
                    for service in \$(docker-compose -p ${COMPOSE_PROJECT_NAME} config --services); do
                        image=\$(docker-compose -p ${COMPOSE_PROJECT_NAME} config | grep "image:.*\$service" | awk '{print \$2}')
                        if [ -n "\$image" ]; then
                            docker tag "\$image" ${DOCKERHUB_REPO}:\$service
                            docker push ${DOCKERHUB_REPO}:\$service
                        fi
                    done

                    docker logout
                    """
                }
            }
        }

        stage('Deploy to Remote Host') {
            steps {
                sshagent(['${SSH_KEY_ID}']) { 
                    sh """
                    ssh -o StrictHostKeyChecking=no funmicra@192.168.88.22 '
                    if docker-compose -p ${COMPOSE_PROJECT_NAME} ps -q | grep -q .; then
                        docker-compose -p ${COMPOSE_PROJECT_NAME} down
                    else
                        echo "No running containers to stop."
                    fi
                        cd /home/funmicra/stacks/docker-update &&
                        docker-compose -p ${COMPOSE_PROJECT_NAME} pull &&
                        docker-compose -p ${COMPOSE_PROJECT_NAME} up -d
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
